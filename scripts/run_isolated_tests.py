"""Run repository Python tests without loading real credentials or business data.

The child process has an allowlisted environment, disables BaseSettings dotenv
loading before importing app code, and rejects Python Internet sockets. PostgreSQL
fixtures still exercise a real temporary Unix-socket-only database.
"""
from __future__ import annotations

import argparse
import atexit
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEST_PATHS = ('backend/tests', 'scripts/tests', 'scripts/recipe_sources/tests')


def clean_environment(source: dict[str, str], temporary: Path) -> dict[str, str]:
    allowed = ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT')
    environment = {key: source[key] for key in allowed if key in source}
    environment.update({
        'LC_ALL': 'C', 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        'PYTHONPATH': os.pathsep.join((str(ROOT), str(ROOT / 'backend'))),
        'APP_ENVIRONMENT': 'test', 'APP_ORIGIN': 'http://127.0.0.1:3000',
        'COOKIE_SECURE': 'false', 'EXTERNAL_CALLS_ENABLED': 'false',
        'DATABASE_URL': '', 'DATA_DIR': str(temporary / 'unused-default-data'),
        'SHUSHI_ISOLATED_TEST_ROOT': str(temporary),
    })
    return environment


def protected_path(value: object) -> bool:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return False
    lexical = Path(os.path.abspath(os.fsdecode(value)))
    # Check both names: .env/data may themselves point outside the checkout,
    # and an innocent-looking temporary symlink may point back into them.
    for path in (lexical, lexical.resolve()):
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            continue
        if relative.parts and (relative.parts[0] == 'data' or
                               any(part.startswith('.env') for part in relative.parts)):
            return True
    return False


def audit_guard(event: str, arguments: tuple) -> None:
    if event == 'open' and arguments and protected_path(arguments[0]):
        raise PermissionError('Isolated tests cannot open repository credentials or business data.')
    if event in {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'}:
        raise PermissionError('Isolated tests cannot perform network name resolution.')
    if event in {'socket.connect', 'socket.connect_ex', 'socket.sendto', 'socket.bind'}:
        connection = arguments[0]
        if connection.family != socket.AF_UNIX:
            raise PermissionError('Isolated tests cannot open Internet sockets; use an in-process fixture.')


def run_worker(collect_only: bool) -> int:
    temporary = Path(os.environ['SHUSHI_ISOLATED_TEST_ROOT']).resolve()
    if not temporary.is_dir() or temporary == ROOT or ROOT in temporary.parents:
        raise SystemExit('Isolated test directory must exist outside the repository.')
    sys.addaudithook(audit_guard)
    from pydantic_settings import BaseSettings

    original_init = BaseSettings.__init__

    def without_dotenv(self, *args, **kwargs):
        kwargs['_env_file'] = None
        return original_init(self, *args, **kwargs)

    BaseSettings.__init__ = without_dotenv
    import pytest

    arguments = [*TEST_PATHS, '--basetemp', str(temporary / 'pytest'), '--tb=short']
    if collect_only:
        arguments.append('--collect-only')
    return int(pytest.main(arguments))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect-only', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker:
        return run_worker(arguments.collect_only)
    temporary = Path(tempfile.mkdtemp(prefix='shushi-isolated-verification-')).resolve()
    # The child exits (including its PostgreSQL atexit stop) before cleanup here.
    atexit.register(shutil.rmtree, temporary, ignore_errors=True)
    command = [sys.executable, str(Path(__file__).resolve()), '--worker']
    if arguments.collect_only:
        command.append('--collect-only')
    print('Python verification: temporary PostgreSQL, no real .env, no Internet sockets.', flush=True)
    return subprocess.call(command, cwd=ROOT, env=clean_environment(os.environ, temporary))


if __name__ == '__main__':
    raise SystemExit(main())
