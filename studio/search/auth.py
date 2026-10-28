"""Authenticate every call to the search service: only the Local Studio backend may read the index.

These routes return the text and bytes of the user's files, so the service is never open, whatever the security.api_token
setting says. The backend keeps the secret in STATE/data/search.token (0600) and passes it in LOCAL_STUDIO_SEARCH_TOKEN
each time it starts the service (studio/services/search.py); every route but /health needs `Authorization: Bearer <it>`.
Loopback alone is not a boundary: another account on this Mac, or a sandboxed app that may not read ~/Documents, can
still reach 127.0.0.1.
"""
import hmac

TOKEN_ENV = "LOCAL_STUDIO_SEARCH_TOKEN"


def authorized(headers, token):
    auth = headers.get(b'authorization', b'').decode('latin-1')
    value = auth[7:] if auth.lower().startswith('bearer ') else ''
    return bool(value and token and hmac.compare_digest(value.encode(), token.encode()))
