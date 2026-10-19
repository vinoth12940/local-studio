"""Opt-in local API authentication. LocalOnly remains the outer access boundary."""
import hmac
import os
import secrets
import tempfile
import threading
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from starlette.responses import JSONResponse, RedirectResponse

from studio.config import DATA
from studio.storage.flags import flag

TOKEN_FILE = DATA / '.api-token'
BOOTSTRAP_FILE = DATA / '.api-bootstrap'


def _private_write(path, value):
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def cookie_name(port):
    return f'local_studio_token_{port}'


class ApiToken:
    def __init__(self, inner):
        self.inner = inner
        self.lock = threading.Lock()
        self.token = None
        self.bootstrap = None
        if flag('security.api_token'):
            self._activate()

    def _activate(self):
        with self.lock:
            if self.token is None:
                token, bootstrap = secrets.token_hex(32), secrets.token_hex(32)
                _private_write(TOKEN_FILE, token)
                _private_write(BOOTSTRAP_FILE, bootstrap)
                self.token, self.bootstrap = token, bootstrap

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.inner(scope, receive, send)
        if not flag('security.api_token'):
            self.token = self.bootstrap = None
            return await self.inner(scope, receive, send)
        self._activate()
        port = (scope.get('server') or ('127.0.0.1', 7860))[1]
        name = cookie_name(port)
        if scope['type'] == 'http':
            query = parse_qs(scope.get('query_string', b'').decode('ascii', 'ignore'))
            ticket = query.get('t', [''])[0]
            if scope['path'] == '/' and ticket:
                # Do not let uvicorn access logs retain even this single-use credential.
                scope['query_string'] = b''
                with self.lock:
                    valid = self.bootstrap is not None and hmac.compare_digest(ticket.encode(), self.bootstrap.encode())
                    if valid:
                        self.bootstrap = None
                        BOOTSTRAP_FILE.unlink(missing_ok=True)
                if valid:
                    response = RedirectResponse('/', status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
                    response.set_cookie(name, self.token, httponly=True, samesite='strict', path='/')
                    return await response(scope, receive, send)
                return await JSONResponse({'detail': 'Invalid or used startup link', 'code': 'invalid_bootstrap'}, status_code=401)(scope, receive, send)
            if scope['path'] in ('/api/system', '/api/health'):
                return await self.inner(scope, receive, send)
        headers = dict(scope.get('headers', []))
        auth = headers.get(b'authorization', b'').decode('latin-1')
        bearer = auth[7:] if auth.lower().startswith('bearer ') else ''
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get(b'cookie', b'').decode('latin-1'))
        except CookieError:
            pass
        cookie = cookies[name].value if name in cookies else ''
        if any(value and hmac.compare_digest(value.encode(), self.token.encode()) for value in (bearer, cookie)):
            return await self.inner(scope, receive, send)
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        return await JSONResponse({'detail': 'Local API authentication required', 'code': 'authentication_required'}, status_code=401,
                                  headers={'Cache-Control': 'no-store', 'WWW-Authenticate': 'Bearer'})(scope, receive, send)
