"""Response headers for every route and static file (refactor P3), as plain ASGI so streams pass through untouched.

- X-Content-Type-Options: nosniff on every response.
- Content-Security-Policy-Report-Only (flag security.csp = "report-only") or Content-Security-Policy (= "enforce"); off by
  default. Violations are posted to /api/csp-report and written to logs/csp.log.
"""
from starlette.responses import PlainTextResponse

from studio.storage.flags import flag

CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
       "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'; report-uri /api/csp-report")


def response_headers(mode):
    headers = {"X-Content-Type-Options": "nosniff"}
    if mode in ("report-only", "enforce"):
        name = "Content-Security-Policy" if mode == "enforce" else "Content-Security-Policy-Report-Only"
        headers[name] = CSP
    return headers


async def internal_error(request, exc):
    # Starlette's ServerErrorMiddleware sits outside user middleware. Preserve its default
    # response body/status while adding the same headers to its otherwise unwrapped 500s.
    return PlainTextResponse("Internal Server Error", status_code=500, headers=response_headers(flag("security.csp")))


class SecurityHeaders:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        mode = flag("security.csp")

        async def send_wrapper(msg):
            if msg["type"] == "http.response.start":
                headers = [(k, v) for k, v in msg.get("headers", []) if k.lower() != b"x-content-type-options"]
                headers.append((b"x-content-type-options", b"nosniff"))
                if mode in ("report-only", "enforce") and not any(k.lower().startswith(b"content-security-policy") for k, _ in headers):
                    name = b"content-security-policy" if mode == "enforce" else b"content-security-policy-report-only"
                    headers.append((name, CSP.encode()))
                msg = {**msg, "headers": headers}
            await send(msg)
        await self.inner(scope, receive, send_wrapper)
