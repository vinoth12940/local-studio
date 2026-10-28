"""Audit pinned requirement sets without creating a project or lockfile in the checkout.

This uv build advertises --script but still requires a project during audit. A temporary pyproject
is verified to work, and keeps resolution/audit failures fatal to release.sh.
"""
import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def audit(requirements, uv):
    pins = [line.strip() for line in requirements.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    with tempfile.TemporaryDirectory(prefix='ls-dependency-audit-', dir='/tmp') as folder:
        root = Path(folder)
        (root / 'pyproject.toml').write_text(
            '[project]\nname = "local-studio-release-audit"\nversion = "0.0.0"\nrequires-python = ">=3.12,<3.13"\ndependencies = [\n'
            + ''.join('  ' + json.dumps(pin) + ',\n' for pin in pins) + ']\n')
        print(f'==> Auditing {requirements.name} ({len(pins)} pinned entries)', flush=True)
        subprocess.run([uv, 'audit', '--preview-features', 'audit', '--project', str(root),
                        '--python-version', '3.12', '--python-platform', 'aarch64-apple-darwin'], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('requirements', nargs='*', type=Path)
    parser.add_argument('--uv', default=shutil.which('uv') or str(Path.home() / '.local/bin/uv'))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    for requirements in args.requirements or [repo / 'requirements-app.txt', repo / 'search-requirements.txt', repo / 'voice-requirements.txt']:
        audit(requirements, args.uv)


if __name__ == '__main__':
    main()
