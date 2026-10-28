"""Opt-in Seatbelt profiles and process resource policy for harness execution only."""
import contextvars
import json
import os
import resource
import signal
import subprocess
import threading
from pathlib import Path

from studio import config
from studio.storage.flags import flag

_python_network = contextvars.ContextVar('python_network', default=False)
# A workflow can require a read-only project even when the optional general
# sandbox is disabled. This is an execution constraint, never an approval grant.
_readonly_workspace = contextvars.ContextVar('readonly_workspace', default=False)

SANDBOX_EXEC = '/usr/bin/sandbox-exec'
DEFAULT_LIMITS = {'cpu_seconds': 30, 'file_bytes': 64 * 2**20, 'open_files': 128, 'rss_bytes': 1024 * 2**20}


def seatbelt_profile(workspace, temp, python=False, network=False, readonly=False):
    workspace, temp = Path(workspace).resolve(), Path(temp).resolve()
    # Avoid accidentally granting writes to an entire volume if a caller selects / or ~.
    if workspace in (Path('/'), config.HOME.resolve()):
        raise ValueError('Sandbox execution requires a specific workspace folder, not / or the home directory.')
    quote = lambda p: json.dumps(str(p), ensure_ascii=False)
    writes = f'(subpath {quote(temp)}) (literal "/dev/null")'
    if not readonly:
        writes = f'(subpath {quote(workspace)}) ' + writes
    lines = ['(version 1)', '(allow default)', '(deny file-write*)',
             f'(allow file-write* {writes})',
             # These regular expressions apply in any directory, including a synthetic test HOME.
             '(deny file-read* (regex #"/(\\.[sS][sS][hH]|\\.[aA][wW][sS]|\\.[gG][nN][uU][pP][gG]|\\.[kK][uU][bB][eE]|[kK][eE][yY][cC][hH][aA][iI][nN][sS]|[pP][aA][sS][sS][wW][oO][rR][dD][sS]|[cC][oO][oO][kK][iI][eE][sS])(/|$)"))',
             '(deny file-read* (regex #"/(\\.env($|\\.)|[^/]*\\.([pP][eE][mM]|[kK][eE][yY]|[pP]12|[pP][fF][xX]|[kK][eE][yY][cC][hH][aA][iI][nN])[^/]*$|id_rsa[^/]*$|id_ed25519[^/]*$|id_ecdsa[^/]*$|\\.npmrc$|\\.pypirc$|\\.netrc$|credentials\\.json$|secrets\\.json$|\\.api-token$|\\.api-bootstrap$|search\\.token$|voice\\.token$)"))',
             '(allow file-read* (regex #"/\\.env\\.(example|sample|template)$"))']
    for suffix in ('Library/Keychains', 'Library/Application Support/Google/Chrome', 'Library/Safari',
                   'Library/Cookies', 'Library/Mail', 'Library/Messages', 'Library/Passwords'):
        lines.append(f'(deny file-read* (subpath {quote(config.HOME / suffix)}))')
    if readonly or (python and not (network or _python_network.get())):
        lines.append('(deny network*)')
    return '\n'.join(lines)


def sandbox_command(argv, workspace, env, python=False, temp=None, network=False):
    readonly = _readonly_workspace.get()
    if not flag('harness.sandbox') and not readonly:
        return argv, env
    if not Path(SANDBOX_EXEC).is_file() or not os.access(SANDBOX_EXEC, os.X_OK):
        raise ValueError('Sandbox requested but /usr/bin/sandbox-exec is unavailable; execution refused.')
    if temp is None:
        raise ValueError('Sandbox execution needs its own temporary directory.')
    env = {**env, 'TMPDIR': str(temp), 'TMP': str(temp), 'TEMP': str(temp)}
    profile = seatbelt_profile(workspace, temp, python=python, network=network, readonly=readonly)
    if readonly:
        # Keep all writes in the new, private execution scratch directory. In
        # particular, test runners cannot rewrite their own project fixtures.
        env = {**env, 'PYTHONDONTWRITEBYTECODE': '1', 'HOME': str(temp),
               'XDG_CACHE_HOME': str(Path(temp) / 'cache')}
    return [SANDBOX_EXEC, '-p', profile, *argv], env


def resource_limits():
    if not flag('harness.rlimits'):
        return None
    # Settings may reduce caps for a constrained Mac; cannot raise the fixed ceilings.
    from studio.storage.settings import load_settings
    opts = load_settings().get('harness', {}).get('limits', {})
    return {key: max(1, min(int(opts.get(key, cap)), cap)) for key, cap in DEFAULT_LIMITS.items()}


def limit_child(limits):
    """Only async-signal-safe setrlimit calls after fork; no locks or settings I/O."""
    def apply():
        for kind, value in ((resource.RLIMIT_CPU, limits['cpu_seconds']), (resource.RLIMIT_FSIZE, limits['file_bytes']),
                            (resource.RLIMIT_NOFILE, limits['open_files'])):
            _, hard = resource.getrlimit(kind)
            cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(kind, (cap, cap))
    return apply


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def start_rss_watchdog(proc, cap):
    """Darwin RLIMIT_AS is unreliable. Sample total RSS for the child's process group."""
    stopped, exceeded = threading.Event(), threading.Event()
    def watch():
        while not stopped.wait(0.1):
            if proc.poll() is not None:
                break
            try:
                output = subprocess.run(['/bin/ps', '-axo', 'pgid=,rss='], capture_output=True, text=True, timeout=2).stdout
                rss = sum(int(row.split()[1]) * 1024 for row in output.splitlines()
                          if len(row.split()) == 2 and row.split()[0] == str(proc.pid))
                if rss > cap:
                    exceeded.set()
                    kill_group(proc)
                    break
            except (OSError, ValueError, subprocess.SubprocessError):
                # RSS is best effort; CPU/file/fd limits are kernel-enforced regardless.
                pass
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return stopped, exceeded
