"""Bound request bytes before multipart parsing can spool an unlimited upload."""

from fastapi import HTTPException
from starlette.responses import JSONResponse


class RequestSizeLimitMiddleware:
    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                scope, receive, send
            )
        if length > self.limit:
            return await JSONResponse({"detail": "Request body exceeds maximum size"}, status_code=413)(
                scope, receive, send
            )
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.limit:
                    raise HTTPException(413, "Request body exceeds maximum size")
            return message

        await self.app(scope, limited_receive, send)
