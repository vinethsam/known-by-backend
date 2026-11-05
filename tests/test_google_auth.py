"""Focused browser authentication checks; no provider calls or databases."""

import base64
import json

import httpx
import pytest

from app.api import auth
from app.api.auth import (
    GOOGLE_CERTIFICATES_URL,
    AuthenticatedUser,
    GoogleAuthenticator,
    GoogleAuthForbidden,
    GoogleAuthRejected,
    GoogleAuthUnavailable,
)

CLIENT_ID = "knownby-test.apps.googleusercontent.com"
CREDENTIAL = "header.payload.signature"
CLAIMS = {
    "sub": "approved-google-sub",
    "email": "person@gmail.com",
    "email_verified": True,
    "name": "Person",
    "exp": 2_000_000_000,
}


def certificate_transport(request):
    assert str(request.url) == GOOGLE_CERTIFICATES_URL
    return httpx.Response(200, json={"key": "certificate"}, headers={"Cache-Control": "public, max-age=3600"})


@pytest.mark.asyncio
async def test_google_identity_verifier_receives_exact_audience_and_reuses_certificates(monkeypatch):
    requests = []

    def transport(request):
        requests.append(request)
        return certificate_transport(request)

    def verify(token, request, audience):
        assert token == CREDENTIAL
        assert audience == CLIENT_ID
        response = request(GOOGLE_CERTIFICATES_URL)
        assert response.status == 200
        assert response.data == b'{"key":"certificate"}'
        return CLAIMS

    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", verify)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        authenticator = GoogleAuthenticator(CLIENT_ID, ["PERSON@gmail.com"], client=client)
        first, second = await authenticator.verify(CREDENTIAL), await authenticator.verify(CREDENTIAL)
    assert (
        first
        == second
        == AuthenticatedUser("approved-google-sub", "person@gmail.com", 2_000_000_000, "Person")
    )
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "someone-else@gmail.com"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"email": "person@example.org"},
        {"email": "person@example.org", "hd": ""},
    ],
)
async def test_google_credentials_do_not_bypass_account_authorization(monkeypatch, overrides):
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *args, **kwargs: CLAIMS | overrides)
    async with httpx.AsyncClient(transport=httpx.MockTransport(certificate_transport)) as client:
        authenticator = GoogleAuthenticator(
            CLIENT_ID, ["person@gmail.com", "person@example.org"], client=client
        )
        with pytest.raises(GoogleAuthForbidden) as error:
            await authenticator.verify(CREDENTIAL)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_google_workspace_verified_email_is_authoritative(monkeypatch):
    monkeypatch.setattr(
        auth.id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: CLAIMS | {"email": "person@example.org", "hd": "example.org"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(certificate_transport)) as client:
        authenticator = GoogleAuthenticator(CLIENT_ID, ["person@example.org"], client=client)
        user = await authenticator.verify(CREDENTIAL)
    assert user.email == "person@example.org"


@pytest.mark.asyncio
async def test_verifier_rejection_is_unauthorized_without_exposing_token(monkeypatch):
    def reject(*args, **kwargs):
        raise ValueError(CREDENTIAL)

    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", reject)
    async with httpx.AsyncClient(transport=httpx.MockTransport(certificate_transport)) as client:
        authenticator = GoogleAuthenticator(CLIENT_ID, ["person@gmail.com"], client=client)
        with pytest.raises(GoogleAuthRejected) as error:
            await authenticator.verify(CREDENTIAL)
    assert error.value.status_code == 401
    assert CREDENTIAL not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_at", [None, True, "2000000000", 2_000_000_000.0])
async def test_expiry_exposed_to_browser_must_be_an_integer(monkeypatch, expires_at):
    monkeypatch.setattr(
        auth.id_token, "verify_oauth2_token", lambda *args, **kwargs: CLAIMS | {"exp": expires_at}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(certificate_transport)) as client:
        authenticator = GoogleAuthenticator(CLIENT_ID, ["person@gmail.com"], client=client)
        with pytest.raises(GoogleAuthRejected):
            await authenticator.verify(CREDENTIAL)


def jwt_segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    [
        CREDENTIAL,
        f"{jwt_segment({'alg': []})}.{jwt_segment({})}.eA",
        f"{jwt_segment({'alg': 'RS256', 'kid': ['key']})}.{jwt_segment({})}.eA",
        f"{jwt_segment(None)}.{jwt_segment({})}.eA",
        f"{jwt_segment({'alg': 'RS256'})}.{jwt_segment([])}.eA",
        f"{jwt_segment({'alg': 'RS256'})}.{jwt_segment({})}.a",
    ],
)
async def test_real_google_verifier_rejects_malformed_credentials_without_server_error(credential):
    async with httpx.AsyncClient(transport=httpx.MockTransport(certificate_transport)) as client:
        authenticator = GoogleAuthenticator(CLIENT_ID, ["person@gmail.com"], client=client)
        with pytest.raises(GoogleAuthRejected) as error:
            await authenticator.verify(credential)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_disabled_authentication_fails_closed_without_network():
    authenticator = GoogleAuthenticator(None, [])
    assert not authenticator.enabled
    with pytest.raises(GoogleAuthUnavailable) as error:
        await authenticator.verify(CREDENTIAL)
    assert error.value.status_code == 503
    await authenticator.close()


@pytest.mark.asyncio
async def test_certificate_provider_failure_is_unavailable_without_exposing_details():
    def fail(request):
        raise httpx.ConnectError("Internal network details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        authenticator = GoogleAuthenticator(CLIENT_ID, ["person@gmail.com"], client=client)
        with pytest.raises(GoogleAuthUnavailable) as error:
            await authenticator.verify(CREDENTIAL)
    assert error.value.status_code == 503
    assert "Internal network details" not in str(error.value)


@pytest.mark.asyncio
async def test_expired_certificate_cache_refreshes(monkeypatch):
    clock = [100.0]
    requests = []
    monkeypatch.setattr(auth, "monotonic", lambda: clock[0])
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *args, **kwargs: CLAIMS)

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"key": "certificate"}, headers={"Cache-Control": "max-age=5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        authenticator = GoogleAuthenticator(CLIENT_ID, ["person@gmail.com"], client=client)
        await authenticator.verify(CREDENTIAL)
        clock[0] += 6
        await authenticator.verify(CREDENTIAL)
    assert len(requests) == 2
