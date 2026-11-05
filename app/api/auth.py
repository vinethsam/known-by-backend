"""Verify per-user Google credentials without sharing the server API token."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from time import monotonic

import httpx
from google.auth.exceptions import GoogleAuthError as GoogleVerificationError
from google.oauth2 import id_token

GOOGLE_CERTIFICATES_URL = "https://www.googleapis.com/oauth2/v1/certs"
MAX_CERTIFICATE_BYTES = 131_072
MAX_CREDENTIAL_LENGTH = 16_384


@dataclass(frozen=True)
class AuthenticatedUser:
    sub: str
    email: str
    expires_at: int
    name: str | None = None


class GoogleAuthError(Exception):
    status_code = 401


class GoogleAuthRejected(GoogleAuthError):
    def __init__(self):
        super().__init__("Invalid or expired Google sign-in credential")


class GoogleAuthForbidden(GoogleAuthError):
    status_code = 403

    def __init__(self):
        super().__init__("This Google account is not approved for KnownBy")


class GoogleAuthUnavailable(GoogleAuthError):
    status_code = 503

    def __init__(self, message="Google sign-in is temporarily unavailable"):
        super().__init__(message)


@dataclass(frozen=True)
class _CertificateResponse:
    # The google-auth transport contract needs only these attributes.
    data: bytes
    status: int = 200


class GoogleAuthenticator:
    def __init__(
        self,
        client_id: str | None,
        allowed_emails: Collection[str],
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.client_id = (client_id or "").strip()
        self.allowed_emails = frozenset(email.strip().casefold() for email in allowed_emails if email.strip())
        self._client = client
        self._owns_client = client is None
        self._certificates: bytes | None = None
        self._certificates_expires = 0.0
        self._certificate_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.allowed_emails)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _get_certificates(self) -> bytes:
        async with self._certificate_lock:
            if self._certificates is not None and monotonic() < self._certificates_expires:
                return self._certificates
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=10, follow_redirects=False)
            try:
                async with self._client.stream("GET", GOOGLE_CERTIFICATES_URL) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_CERTIFICATE_BYTES:
                            raise ValueError("Certificate response too large")
                    certificates = json.loads(content)
                    if (
                        not isinstance(certificates, dict)
                        or not certificates
                        or not all(
                            isinstance(key, str) and isinstance(value, str)
                            for key, value in certificates.items()
                        )
                    ):
                        raise ValueError("Invalid certificate response")
                    cache_control = response.headers.get("cache-control", "")
            except (httpx.HTTPError, ValueError, UnicodeError):
                raise GoogleAuthUnavailable() from None
            max_age = re.search(r"(?:^|,)\s*max-age\s*=\s*(\d+)(?:\s*,|\s*$)", cache_control, re.I)
            ttl = min(int(max_age.group(1)), 3600) if max_age else 300
            if re.search(r"(?:^|,)\s*(?:no-store|no-cache)(?:\s*,|\s*$)", cache_control, re.I):
                ttl = 0
            self._certificates = bytes(content)
            self._certificates_expires = monotonic() + ttl
            return self._certificates

    async def verify(self, credential: str) -> AuthenticatedUser:
        if not self.enabled:
            raise GoogleAuthUnavailable("Google sign-in is not configured")
        if not credential or len(credential) > MAX_CREDENTIAL_LENGTH or credential.count(".") != 2:
            raise GoogleAuthRejected()
        certificates = await self._get_certificates()

        def certificate_request(url, method="GET", **kwargs):
            if url != GOOGLE_CERTIFICATES_URL or method != "GET":
                raise GoogleAuthUnavailable()
            return _CertificateResponse(certificates)

        try:
            # google-auth validates Google's signature, audience, issuer and
            # time claims. Its synchronous transport uses the already fetched
            # certificates, so verification performs no blocking network I/O.
            claims = id_token.verify_oauth2_token(credential, certificate_request, audience=self.client_id)
        except (ValueError, TypeError, GoogleVerificationError):
            raise GoogleAuthRejected() from None

        email = claims.get("email")
        subject = claims.get("sub")
        expires_at = claims.get("exp")
        if (
            not isinstance(email, str)
            or not isinstance(subject, str)
            or not subject
            or type(expires_at) is not int
        ):
            raise GoogleAuthRejected()
        email = email.strip().casefold()
        hosted_domain = claims.get("hd")
        authoritative = email.endswith("@gmail.com") or (
            isinstance(hosted_domain, str) and bool(hosted_domain.strip())
        )
        # Google is not authoritative for third-party email accounts without
        # Workspace's hd claim, even if email_verified was true at registration.
        if claims.get("email_verified") is not True or not authoritative or email not in self.allowed_emails:
            raise GoogleAuthForbidden()
        name = claims.get("name")
        return AuthenticatedUser(subject, email, expires_at, name if isinstance(name, str) else None)
