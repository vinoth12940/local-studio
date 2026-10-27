"""Search service: crawler. Moved verbatim from search.py (P6)."""
import fnmatch
import os
import stat
from pathlib import Path

from studio.search import config as config_module
from studio.search.config import HF_HUB, STATE, _under

# ---------------------------------------------------------------- crawler
# Only these extensions have their content read; everything else is indexed by name alone.

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
            ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java",
            ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".zsh", ".sql", ".toml",
            ".ini", ".cfg", ".log", ".r", ".m", ".scala", ".lua", ".pl", ".dart", ".vue", ".svelte"}   # same list as app.py
DOC_EXT = TEXT_EXT | {".pdf", ".docx", ".xlsx", ".xlsm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aiff", ".aif", ".caf", ".flac"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "build", "dist", "target", "DerivedData", "Pods",
             "site-packages", "dist-packages"}
PACKAGE_SUFFIXES = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary", ".bundle",
                    ".framework", ".xcodeproj", ".xcworkspace")
SECRET_GLOBS = ("*.pem", "*.p12", "*.keychain*", "id_rsa*", "id_ed25519*")   # .env* is hidden, so already skipped
MAX_TEXT_BYTES = 5 * 2 ** 20
MAX_DOC_BYTES = 100 * 2 ** 20
SF_DATALESS = 0x40000000          # an iCloud placeholder: reading it would download the file


def classify(name, size):
    ext = Path(name).suffix.lower()
    if ext in TEXT_EXT:
        return "document" if size <= MAX_TEXT_BYTES else "name"
    if ext in DOC_EXT:
        return "document" if size <= MAX_DOC_BYTES else "name"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return "name"


def _dataless(st):
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def _python_env(path):
    """A virtual or conda environment, whatever it is called: thousands of library files, none the user's."""
    return os.path.exists(os.path.join(path, "pyvenv.cfg")) or os.path.isdir(os.path.join(path, "conda-meta"))


# Not node_modules: installed packages often have a nested one, and a project that ran an install has a lockfile.
PROJECT_MARKERS = (".git", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")
PACKAGE_DUMP_MIN = 5              # this many installed packages side by side means "the contents of a node_modules"


def _installed_package(path):
    """An npm package as it sits in node_modules: a package.json, and none of the things a project
    someone is working on has (a lockfile, git)."""
    return (os.path.exists(os.path.join(path, "package.json"))
            and not any(os.path.exists(os.path.join(path, m)) for m in PROJECT_MARKERS))


def _package_dump(entries):
    """Names of the sub-folders that are installed npm packages, when there are enough of them side
    by side to be sure this folder is a node_modules under another name. One or two folders with a
    package.json are far more likely to be the user's own, so those are left alone."""
    dirs = []
    for e in entries:
        try:
            if not e.name.startswith(".") and not e.is_symlink() and e.is_dir(follow_symlinks=False):
                dirs.append(e)
        except OSError:
            pass
    found = {e.name for e in dirs if not e.name.startswith("@") and _installed_package(e.path)}
    if len(found) < PACKAGE_DUMP_MIN:
        return set()
    for e in dirs:                                        # scopes: @babel/core, @types/node, ...
        if e.name.startswith("@"):
            try:
                subs = [x.path for x in os.scandir(e.path) if x.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            if subs and all(_installed_package(x) for x in subs):
                found.add(e.name)
    return found


def walk(roots, removed=(), no_access=None, failed=None):
    """Yield (path, size, mtime, kind) once for every file worth indexing under `roots`. Kind is
    "placeholder" for an iCloud file that isn't downloaded. Roots macOS won't let us list go to
    `no_access`; every folder that couldn't be listed (denied, unplugged, missing) goes to `failed`,
    so the caller can keep what it already knows about the files inside."""
    app_data = [str(STATE / d) for d in ("data", "uploads", "logs", "gallery")] + [str(HF_HUB)]
    library = str(config_module.HOME / "Library")
    roots = list(dict.fromkeys(str(r).rstrip("/") or "/" for r in roots))
    root_set = set(roots)
    for root in roots:
        # A folder the user added is indexed even inside one they removed, and even under ~/Library
        # (iCloud Drive, ~/Library/CloudStorage); walking down from above never enters either.
        gone = [x for x in removed if not _under(root, x)]
        in_library = _under(root, library)

        def off_limits(path):
            return (any(_under(path, b) for b in app_data) or any(_under(path, x) for x in gone)
                    or (not in_library and _under(path, library)))

        if off_limits(root):
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError as err:
                if failed is not None:
                    failed.append(d)
                if no_access is not None and d == root and isinstance(err, PermissionError):
                    no_access.append(d)
                continue
            dumped = _package_dump(entries)
            for e in entries:
                try:
                    if e.name.startswith(".") or e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if (e.path not in root_set                   # another root: its own walk covers it
                                and e.name not in dumped
                                and e.name not in SKIP_DIRS and not e.name.lower().endswith(PACKAGE_SUFFIXES)
                                and not off_limits(e.path) and not _python_env(e.path)):
                            stack.append(e.path)
                        continue
                    if any(fnmatch.fnmatch(e.name, g) for g in SECRET_GLOBS):
                        continue
                    st = e.stat(follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    yield e.path, st.st_size, st.st_mtime, "placeholder" if _dataless(st) else classify(e.name, st.st_size)
                except OSError:
                    continue
