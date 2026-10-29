"""The FastAPI app: LocalOnly on every route and static file, the /gallery mount, and one APIRouter per area.

Assembled in the refactor's P1 split. app.py is the entrypoint `uvicorn app:app` loads.
"""
import importlib
import sys

from fastapi import FastAPI

from studio.api import agent as api_agent
from studio.api import attachments as api_attachments
from studio.api import chat as api_chat
from studio.api import chats as api_chats
from studio.api import engines as api_engines
from studio.api import features as api_features
from studio.api import images as api_images
from studio.api import mcp as api_mcp
from studio.api import memory as api_memory
from studio.api import models as api_models
from studio.api import processes as api_processes
from studio.api import search as api_search
from studio.api import voice as api_voice
from studio.api import storage as api_storage
from studio.api import system as api_system
from studio.api import terminal as api_terminal
from studio.api import undo as api_undo
from studio.api import updates as api_updates
from studio.api import workspace as api_workspace
from studio.api import reviews as api_reviews
from studio.api import triggers as api_triggers
from studio.api import workflow as api_workflow
from studio.api import worktrees as api_worktrees
from studio.api import workflow_runs as api_workflow_runs
from studio.config import GALLERY, ROOT
from studio.infra.logs import install_log_redaction
from studio.infra.request_log import RequestLog
from studio.infra.static import FreshStaticFiles, MediaStaticFiles
from studio.security.headers import SecurityHeaders, internal_error
from studio.security.local_only import LocalOnly
from studio.security.api_token import ApiToken

# Every UI backend module, in dependency order; the standalone studio.search and studio.voice services have their own entrypoints.
MODULES = (
    "studio.config",
    "studio.security.redact",
    "studio.security.uploads",
    "studio.storage.atomic",
    "studio.infra.logs",
    "studio.infra.sse",
    "studio.infra.system",
    "studio.infra.text",
    "studio.infra.edits",
    "studio.infra.harness_args",
    "studio.infra.workspace",
    "studio.security.execution",
    "studio.infra.workers",
    "studio.infra.processes",
    "studio.infra.mcp_client",
    "studio.security.fences",
    "studio.storage.settings",
    "studio.storage.memory",
    "studio.storage.backups",
    "studio.storage.chats",
    "studio.storage.chat_index",
    "studio.storage.flags",
    "studio.security.workflow_policy",
    "studio.infra.request_log",
    "studio.infra.static",
    "studio.security.local_only",
    "studio.security.headers",
    "studio.security.api_token",
    "studio.services.models",
    "studio.services.engines",
    "studio.api.models",
    "studio.api.engines",
    "studio.services.attachments",
    "studio.api.attachments",
    "studio.services.images",
    "studio.api.images",
    "studio.services.search",
    "studio.api.search",
    "studio.services.decide",
    "studio.services.voice",
    "studio.api.voice",
    "studio.services.terminal",
    "studio.api.terminal",
    "studio.security.agent_gate",
    "studio.services.web",
    "studio.services.agent.cua",
    "studio.services.agent.views",
    "studio.services.mcp_custom",
    "studio.security.harness_gate",
    "studio.services.agent.loop",
    "studio.api.agent",
    "studio.services.harness",
    "studio.services.instructions",
    "studio.services.skills",
    "studio.services.workflow",
    "studio.services.workflow_runs",
    "studio.services.verification",
    "studio.services.reviews",
    "studio.services.triggers",
    "studio.services.worktrees",
    "studio.services.chat_streams",
    "studio.api.memory",
    "studio.api.undo",
    "studio.api.processes",
    "studio.api.mcp",
    "studio.api.workspace",
    "studio.api.workflow",
    "studio.api.reviews",
    "studio.api.triggers",
    "studio.api.worktrees",
    "studio.api.workflow_runs",
    "studio.api.chat",
    "studio.api.chats",
    "studio.api.system",
    "studio.services.updates",
    "studio.api.updates",
    "studio.api.storage",
    "studio.api.features",
)
for _m in MODULES:
    importlib.import_module(_m)

install_log_redaction()
app = FastAPI()
app.add_exception_handler(Exception, internal_error)
app.mount("/gallery", MediaStaticFiles(directory=GALLERY), name="gallery")
app.mount("/frontend", FreshStaticFiles(directory=ROOT / "frontend"), name="frontend")   # the UI's ES modules (read-only code)
app.add_middleware(RequestLog)                               # inside LocalOnly; does nothing unless logging.json_requests
app.add_middleware(ApiToken)
app.add_middleware(LocalOnly)                                # every route and static file
app.add_middleware(SecurityHeaders)                          # outermost: nosniff (and the CSP flag) on every response, 403s too
# In the order the routes had in app.py. No two routes can match the same request (macos/check_app.py checks that their
# order is kept if two ever could), so the order between areas does not change which route answers.
for _router in (api_system.router, api_models.router, api_engines.router, api_chat.router, api_memory.router, api_undo.router,
                api_processes.router, api_terminal.router, api_mcp.router, api_workspace.router, api_attachments.router,
                api_chats.router, api_images.router, api_search.router, api_agent.router, api_updates.router,
                api_storage.router, api_workflow.router, api_reviews.router, api_triggers.router, api_worktrees.router, api_workflow_runs.router,
                api_voice.router, api_features.router):
    app.include_router(_router)


def owner_module(name):
    """The studio module that defines `name`: the first in dependency order that has it (later ones can only import it)."""
    for m in MODULES:
        mod = sys.modules[m]
        if name in vars(mod):
            return mod
    return None
