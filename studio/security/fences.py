"""Path and shell-command fences of the agent harness.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import fnmatch
import re
import shlex
from pathlib import Path

from studio import config
from studio.config import AGENT_BACKUPS_DIR, AGENT_MEMORY_FILE, CHATS, HOME, SETTINGS
from studio.infra.workspace import _harness_root


# -- Agent Harness helpers: local-model argument repair, path/command safety fences, memory, and interactive chat sessions

_HARNESS_SKIP_DIRS = frozenset((".git", "node_modules", "venv", ".venv", "__pycache__", "build", "dist", "target", "DerivedData", "Pods", "site-packages", ".Trash"))
_HARNESS_SENSITIVE_NAMES = frozenset((".ssh", ".aws", ".gnupg", ".kube", ".npmrc", ".pypirc", ".netrc", "Keychains", "Passwords", "Cookies"))
_HARNESS_SECRET_GLOBS = ("*.pem", "*.p12", "*.pfx", "*.keychain*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "credentials.json", "secrets.json")
_HARNESS_ALLOWED_ENV = frozenset((".env.example", ".env.sample", ".env.template"))
_HARNESS_SYS_PREFIXES = ("/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/var", "/private/etc", "/Applications")


_SHELL_BLOCKED_RE = re.compile(
    r"(?:^|[;&|`\n\s])(?:sudo|su|doas|csrutil|diskutil|launchctl|shutdown|reboot|halt|mkfs)\b"
    r"|\bsecurity\s+(?:find-|dump-|delete-|export|unlock-)"
    r"|\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:/(?:\s|$)|~(?:/|\s|$)|\$HOME\b|\*(?:\s|$))"
    r"|\b(?:curl|wget)\b[^|\n]*\|\s*(?:sh|bash|zsh|python\d*)\b"
    r"|\bdd\s+if="
    r"|:\(\)\s*\{"
    r"|\b(?:chmod|chown)\b[^;\n]+(?:/etc|/System|/usr|/bin|/sbin|/Library|/var|/private)"
    r"|(?:/|\b)(?:\.ssh|\.aws|\.gnupg|Keychains)(?:/|\b)", re.I)


def _harness_path_fence(raw_path, write=False) -> Path:
    """Resolve a local path and reject credentials, private keys, browser profiles, or system directories."""
    if raw_path is None or (isinstance(raw_path, str) and not raw_path.strip()):
        if write:
            raise ValueError("A file path is required")
        return _harness_root()
    if not isinstance(raw_path, str) or "\x00" in raw_path or len(raw_path) > 1024:
        raise ValueError("Invalid file path")
    p = Path(raw_path.strip()).expanduser()
    if not p.is_absolute():
        p = _harness_root() / p
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p.absolute()
    parts = set(resolved.parts) | set(p.parts)
    if parts & _HARNESS_SENSITIVE_NAMES:
        raise ValueError(f"Access to sensitive directory ({next(iter(parts & _HARNESS_SENSITIVE_NAMES))}) is blocked")
    name_low = resolved.name.lower()
    if (name_low == ".env" or (name_low.startswith(".env.") and name_low not in _HARNESS_ALLOWED_ENV)):
        raise ValueError("Access to .env secret files is blocked")
    if any(fnmatch.fnmatch(name_low, g) for g in _HARNESS_SECRET_GLOBS):
        raise ValueError(f"Access to credential or key file ({resolved.name}) is blocked")
    res_str = str(resolved)
    for blocked_dir in (HOME / "Library/Keychains", HOME / "Library/Application Support/Google/Chrome",
                        HOME / "Library/Safari", HOME / "Library/Cookies", HOME / "Library/Mail",
                        HOME / "Library/Messages", HOME / "Library/Passwords"):
        b_str = str(blocked_dir)
        if res_str == b_str or res_str.startswith(b_str + "/"):
            raise ValueError("Access to protected macOS user data is blocked")
    if write:
        for sys_prefix in _HARNESS_SYS_PREFIXES:
            if res_str == sys_prefix or res_str.startswith(sys_prefix + "/"):
                raise ValueError(f"Writing to system path ({sys_prefix}) is blocked")
        if any(part.lower().endswith(".app") for part in resolved.parts):
            raise ValueError("Writing inside macOS application bundles (.app) is blocked")
        for internal in (SETTINGS, CHATS, config.AGENT_RUNS, AGENT_MEMORY_FILE, AGENT_BACKUPS_DIR):
            i_str = str(internal.resolve() if internal.exists() else internal.absolute())
            if res_str == i_str or res_str.startswith(i_str + "/"):
                raise ValueError("Modifying Local Studio internal state files directly is blocked")
    return resolved


def _gate_shell_command(cmd):
    if not isinstance(cmd, str) or not cmd.strip():
        return False, "run_shell requires a non-empty command string."
    if len(cmd) > 4000 or "\x00" in cmd:
        return False, "Command is too long or contains invalid control characters."
    if _SHELL_BLOCKED_RE.search(cmd):
        return False, "This shell command is blocked because it accesses privileged, destructive, or credential targets."
    return True, "Runs a local shell command in the workspace."


_SAFE_READONLY_BINS = frozenset(("ls", "pwd", "which", "whoami", "uname", "date", "wc", "head", "tail", "cat", "stat", "file"))
_SAFE_GIT_SUBCMDS = frozenset(("status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame", "shortlog"))


def _is_safe_readonly_shell(cmd: str) -> bool:
    """True when cmd is a single, non-chained read-only inspection or test verification command."""
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    ok, _ = _gate_shell_command(cmd)
    if not ok:
        return False
    s = cmd.strip()
    if any(op in s for op in (";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r", "&")):
        return False
    try:
        tokens = shlex.split(s)
    except ValueError:
        return False
    if not tokens:
        return False
    # Check any path-like arguments against the path fence so cat ~/.ssh/id_rsa is never auto-approved
    for tok in tokens[1:]:
        if not tok.startswith("-") and ("/" in tok or tok.startswith("~") or tok.startswith(".")):
            try:
                _harness_path_fence(tok, write=False)
            except ValueError:
                return False
    prog = Path(tokens[0]).name
    if prog == "git":
        if len(tokens) < 2:
            return False
        # Skip harmless global flags like --no-pager
        idx = 1
        while idx < len(tokens) and tokens[idx] in ("--no-pager",):
            idx += 1
        if idx >= len(tokens):
            return False
        sub = tokens[idx]
        if sub not in _SAFE_GIT_SUBCMDS:
            return False
        if sub == "branch" and any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move") for a in tokens[idx + 1:]):
            return False
        if any(a.startswith(("--output", "--ext-diff", "--exec", "-c")) for a in tokens[idx + 1:]):
            return False
        return True
    if prog in _SAFE_READONLY_BINS:
        return True
    if prog in ("pytest", "py.test"):
        return True
    if prog in ("python", "python3") and len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in ("pytest", "unittest", "py_compile"):
        return True
    if prog == "node" and len(tokens) == 3 and tokens[1] in ("--check", "-c"):
        return True
    if prog == "cargo" and len(tokens) >= 2 and tokens[1] in ("check", "test", "clippy"):
        return True
    if prog == "go" and len(tokens) >= 2 and tokens[1] in ("test", "vet"):
        return True
    if prog == "npm" and len(tokens) >= 2 and (tokens[1] in ("test", "t") or (tokens[1] == "run" and len(tokens) >= 3 and tokens[2] in ("test", "lint", "check", "typecheck"))):
        return True
    return False
