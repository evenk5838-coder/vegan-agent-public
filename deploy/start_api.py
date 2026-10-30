"""Production API entrypoint: validate -> DB ready -> Alembic -> one Uvicorn worker.

No app/core import occurs before DATABASE_URL has been set. Never prints env,
DSNs, provider errors, full subprocess output, or credentials.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

from preflight import PreflightError, validate_runtime


def database_url(environ: dict[str, str]) -> str:
    from sqlalchemy.engine import URL
    return URL.create('postgresql+psycopg', username=environ.get('POSTGRES_USER', 'shushi'),
                      password=environ['POSTGRES_PASSWORD'], host=environ.get('POSTGRES_HOST', 'db'),
                      port=int(environ.get('POSTGRES_PORT', '5432')),
                      database=environ.get('POSTGRES_DB', 'shushi')).render_as_string(hide_password=False)


def wait_for_database(url: str, timeout_seconds: int = 90) -> None:
    from sqlalchemy import create_engine, text
    engine = create_engine(url, pool_pre_ping=True, connect_args={'connect_timeout': 5}, echo=False)
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            try:
                with engine.connect() as connection:
                    connection.execute(text('SELECT 1'))
                return
            except Exception:
                time.sleep(2)
        raise PreflightError('数据库等待超时，API未启动；请检查私有数据库健康状态。')
    finally:
        engine.dispose()


def main() -> int:
    try:
        validate_runtime(dict(os.environ), container=True)
        # Reject externally supplied DSNs so no development/other-product DB is selected.
        if os.environ.get('DATABASE_URL'):
            raise PreflightError('本部署包不接受额外 DATABASE_URL，请使用专属私有数据库配置。')
        os.environ['DATABASE_URL'] = database_url(dict(os.environ))
        folder = Path('/srv/data')
        if folder.is_symlink():
            raise PreflightError('数据目录不能是符号链接。')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        print('启动预检通过；等待私有数据库。', flush=True)
        wait_for_database(os.environ['DATABASE_URL'])
        print('数据库已就绪；应用数据库迁移。', flush=True)
        migration = subprocess.run([sys.executable, '-m', 'alembic', '-c', '/srv/backend/alembic.ini', 'upgrade', 'head'],
                                   cwd='/srv', capture_output=True, timeout=120)
        if migration.returncode:
            raise PreflightError('数据库迁移失败，API未启动；未输出可能含连接信息的原始日志。')
        print('迁移完成；以单一worker启动API。', flush=True)
        # Caddy overwrites X-Forwarded-For; API has no published host port. Never expose 8000.
        os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'app.main:app',
                                 '--app-dir', '/srv/backend', '--host', '0.0.0.0', '--port', '8000',
                                 '--workers', '1', '--proxy-headers', '--forwarded-allow-ips', '*',
                                 '--no-access-log', '--log-level', 'warning'])
        return 1
    except PreflightError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('生产启动未完成；API保持停止，未输出运行配置或底层异常。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
