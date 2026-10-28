"""Server-sent event framing.

Moved from app.py in the refactor's P1 split.
"""

import json


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"
