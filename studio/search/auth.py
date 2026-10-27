"""Authenticate internal search calls when the UI's local API token flag is enabled."""
import hmac
import json
from studio.search import config


def authorized(headers):
    try:
        enabled = json.loads(config.SETTINGS.read_text()).get('security', {}).get('api_token', False)
    except FileNotFoundError:
        return True
    except (OSError, ValueError, AttributeError):
        return False  # malformed authentication settings must not open the service
    if enabled is not True:
        return True
    try:
        token = (config.STATE / 'data/.api-token').read_text().strip()
    except OSError:
        return False
    auth = headers.get(b'authorization', b'').decode('latin-1')
    value = auth[7:] if auth.lower().startswith('bearer ') else ''
    return bool(value and token and hmac.compare_digest(value.encode(), token.encode()))
