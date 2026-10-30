"""Private PostgreSQL/media backup and isolated restore; no destructive clean mode.

No app/core import, DSN output, cloud upload, application worker or provider call.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
TABLES = {'users', 'sessions', 'records', 'jobs', 'alembic_version'}


class OpsError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def configuration(config_file: Path | None = None, explicit_target: bool = False) -> dict:
    from dotenv import dotenv_values
    from sqlalchemy.engine import make_url
    source = config_file if config_file is not None else PROJECT / '.env'
    values = {}
    if source.exists():
        if source.is_symlink() or not source.is_file():
            raise OpsError('配置必须是普通文件。')
        if explicit_target and source.stat().st_mode & 0o077:
            raise OpsError('恢复目标配置必须由所有者设为600。')
        # Pydantic's existing Settings source uses python-dotenv interpolation too.
        values = {str(k).upper(): str(v or '') for k, v in dotenv_values(source).items()}
    elif config_file is not None:
        raise OpsError('指定配置文件不存在。')
    if explicit_target and (not values.get('DATABASE_URL') or not values.get('DATA_DIR')):
        raise OpsError('恢复目标配置必须明确填写DATABASE_URL与DATA_DIR。')
    values.update({k: os.environ[k] for k in ('DATA_DIR', 'DATABASE_URL') if k in os.environ})
    data = Path(values.get('DATA_DIR') or PROJECT / 'data').expanduser()
    if explicit_target and data.is_symlink():
        raise OpsError('恢复目标媒体目录不能是符号链接。')
    data = (PROJECT / data).resolve() if not data.is_absolute() else data.resolve()
    url = values.get('DATABASE_URL', '')
    if url:
        parsed = make_url(url)
        if parsed.drivername not in {'postgres', 'postgresql', 'postgresql+psycopg'}:
            raise OpsError('只支持项目现有的PostgreSQL连接。')
        allowed = {'host', 'port', 'sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'connect_timeout'}
        if set(parsed.query) - allowed:
            raise OpsError('数据库连接包含此备份工具未支持的参数，未继续操作。')
        params = {'host': parsed.host or '127.0.0.1', 'port': str(parsed.port or 5432),
                  'dbname': parsed.database or 'postgres', 'user': parsed.username or 'postgres',
                  'password': parsed.password or '', **dict(parsed.query)}
    else:
        pgdata = data / 'postgres'
        if not (pgdata / 'PG_VERSION').is_file():
            raise OpsError('配置指向的本地数据库尚未初始化；未回退到其他库。')
        socket = Path('/private/tmp') / ('shushi-pg-' + fingerprint(str(pgdata))[:16])
        params = {'host': str(socket), 'port': '5432', 'dbname': 'postgres', 'user': 'postgres', 'password': ''}
    identity = {k: params[k] for k in ('host', 'port', 'dbname', 'user')}
    return {'params': params, 'data': data, 'fingerprint': fingerprint(json.dumps(identity, sort_keys=True)),
            'data_fingerprint': fingerprint(str(data))}


def pg_tool(name: str) -> str:
    folder = os.environ.get('PG_BINDIR')
    if folder:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise OpsError('PG_BINDIR 缺少必需的PostgreSQL工具。')
    try:
        from pgserver._commands import POSTGRES_BIN_PATH
        candidate = POSTGRES_BIN_PATH / name
        if candidate.is_file():
            return str(candidate)
    except ImportError:
        pass
    candidate = shutil.which(name)
    if candidate:
        return candidate
    raise OpsError('缺少与数据库版本匹配的PostgreSQL备份/恢复工具。')


@contextmanager
def pg_environment(config: dict):
    """Short-lived 0600 password file; never put passwords in CLI arguments."""
    p = config['params']
    with tempfile.TemporaryDirectory(prefix='shushi-pgpass-') as folder:
        password_file = Path(folder) / 'pgpass'
        escaped = str(p.get('password', '')).replace('\\', '\\\\').replace(':', '\\:')
        password_file.write_text('*:*:*:*:' + escaped + '\n')
        password_file.chmod(0o600)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C',
               'PGHOST': str(p['host']), 'PGPORT': str(p['port']), 'PGDATABASE': str(p['dbname']),
               'PGUSER': str(p['user']), 'PGPASSFILE': str(password_file), 'PGCONNECT_TIMEOUT': '5'}
        for key in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey'):
            if key in p:
                env['PG' + key.upper()] = str(p[key])
        yield env


def run_pg(name: str, arguments: list[str], config: dict | None = None, timeout=180) -> None:
    if config is None:
        result = subprocess.run([pg_tool(name), *arguments], capture_output=True, timeout=timeout,
                                env={**os.environ, 'LC_ALL': 'C'})
    else:
        with pg_environment(config) as env:
            result = subprocess.run([pg_tool(name), *arguments], capture_output=True, timeout=timeout, env=env)
    if result.returncode:
        raise OpsError(f'{name} 未完成；为保护连接信息和原始数据，未输出底层日志。')


def connect(config: dict):
    import psycopg
    return psycopg.connect(**{**config['params'], 'connect_timeout': 5})


def counts(connection) -> dict[str, int]:
    from psycopg import sql
    tables = [row[0] for row in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall()]
    if set(tables) - TABLES:
        raise OpsError('连接含本项目之外的公共数据表，未继续操作。')
    return {table: connection.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
            for table in sorted(tables)}


def backup(output: Path, config: dict) -> dict:
    from psycopg import IsolationLevel
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(config['data']):
        raise OpsError('备份目标必须尚不存在，且不能位于源数据目录内。')
    if output.resolve().is_relative_to(PROJECT):
        raise OpsError('私有备份必须保存在项目目录以外。')
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    archive = output / 'database.dump'
    with connect(config) as connection:
        connection.isolation_level = IsolationLevel.REPEATABLE_READ
        connection.read_only = True
        snapshot = connection.execute('SELECT pg_export_snapshot()').fetchone()[0]
        row_counts = counts(connection)
        run_pg('pg_dump', ['--format=custom', '--no-owner', '--no-privileges', '--schema=public',
                           '--snapshot=' + snapshot, '--file=' + str(archive)], config)
    archive.chmod(0o600)
    files = []
    for kind in ('images', 'audio'):
        folder = config['data'] / kind
        if folder.is_symlink():
            raise OpsError('私有媒体目录包含符号链接，未复制。')
        if not folder.exists():
            continue
        for source in sorted(folder.iterdir()):
            if source.is_symlink() or not source.is_file():
                raise OpsError('私有媒体目录包含非普通文件，未复制。')
            relative = PurePosixPath('media', kind, source.name)
            target = output.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            before = sha256(source)
            shutil.copyfile(source, target)
            target.chmod(0o600)
            if sha256(target) != before or sha256(source) != before:
                raise OpsError('媒体在备份中发生变化；本次备份未标为完成，请停止API写入后重试。')
            files.append({'path': str(relative), 'bytes': target.stat().st_size, 'sha256': before})
    files.append({'path': 'database.dump', 'bytes': archive.stat().st_size, 'sha256': sha256(archive)})
    manifest = {'format': 'shushi-private-backup', 'version': 1,
                'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'complete',
                'source_fingerprint': config['fingerprint'], 'source_data_fingerprint': config['data_fingerprint'],
                'database_counts': row_counts, 'files': files,
                'media_consistency': 'individual_file_verified__stop_api_for_cross_resource_consistency'}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (output / 'manifest.json').chmod(0o600)
    verify_backup(output)
    return {'operation': 'backup', 'manifest': 'verified', 'database_counts': row_counts,
            'media_files': len(files) - 1, 'database_snapshot': True, 'media_atomic_with_database': False}


def manifest_path(root: Path, value: str) -> Path:
    if not isinstance(value, str):
        raise OpsError('备份清单路径格式错误。')
    relative = PurePosixPath(value)
    if (relative.is_absolute() or '\\' in value or any(part in {'', '.', '..'} for part in value.split('/'))
            or not (value == 'database.dump' or (len(relative.parts) == 3 and relative.parts[:2] in
                    {('media', 'images'), ('media', 'audio')}))):
        raise OpsError('备份清单包含不允许的路径。')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise OpsError('备份清单文件不能经由符号链接。')
    if not path.resolve().is_relative_to(root.resolve()):
        raise OpsError('备份清单路径越界。')
    return path


def verify_backup(source: Path) -> dict:
    if source.is_symlink() or not source.is_dir():
        raise OpsError('备份源必须是普通目录。')
    manifest_file = source / 'manifest.json'
    if manifest_file.is_symlink() or not manifest_file.is_file() or manifest_file.stat().st_size > 5_000_000:
        raise OpsError('备份清单缺失或不合法；不自动采用旧版无校验备份。')
    manifest = json.loads(manifest_file.read_text())
    if (manifest.get('format') != 'shushi-private-backup' or manifest.get('version') != 1
            or manifest.get('status') != 'complete' or not isinstance(manifest.get('files'), list)
            or not isinstance(manifest.get('database_counts'), dict) or set(manifest['database_counts']) - TABLES):
        raise OpsError('备份清单版本、状态或数据表不合法。')
    if (not manifest['database_counts'] or any(type(n) is not int or n < 0 for n in manifest['database_counts'].values())
            or any(not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get(key, '')))
                   for key in ('source_fingerprint', 'source_data_fingerprint'))
            or len(manifest['files']) > 100000):
        raise OpsError('备份行数、来源指纹或文件清单不合法。')
    seen = set()
    for item in manifest['files']:
        if not isinstance(item, dict) or type(item.get('bytes')) is not int or item['bytes'] < 0 or not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', ''))):
            raise OpsError('备份文件清单格式错误。')
        value = item.get('path')
        path = manifest_path(source, value)
        if value in seen or not path.is_file() or path.stat().st_size != item.get('bytes') or sha256(path) != item.get('sha256'):
            raise OpsError('备份文件数量、大小或校验和不一致。')
        seen.add(value)
    if 'database.dump' not in seen:
        raise OpsError('备份清单缺少数据库归档。')
    actual = {str(path.relative_to(source).as_posix()) for path in source.rglob('*') if path.is_file()}
    if actual != seen | {'manifest.json'}:
        raise OpsError('备份目录存在未列入清单的文件。')
    if any(path.is_symlink() for path in source.rglob('*')):
        raise OpsError('备份目录包含符号链接。')
    return manifest


@contextmanager
def temporary_postgres():
    """Fresh, private Unix-socket-only cluster; never starts a real project DB."""
    short_tmp = os.environ.get('OPS_TEMP_DIR') or ('/private/tmp' if Path('/private/tmp').is_dir() else '/tmp')
    with tempfile.TemporaryDirectory(prefix='shushi-restore-', dir=short_tmp) as name:
        folder = Path(name).resolve()
        data, socket = folder / 'postgres', folder / 'socket'
        socket.mkdir(mode=0o700)
        run_pg('initdb', ['-D', str(data), '--encoding=UTF8', '--locale=C', '--auth-local=trust',
                          '--auth-host=scram-sha-256', '-U', 'postgres'])
        started = False
        try:
            run_pg('pg_ctl', ['-D', str(data), '-w', '-l', str(folder / 'server.log'), '-o',
                             "-h '' -k " + shlex.quote(str(socket)), 'start'])
            started = True
            params = {'host': str(socket), 'port': '5432', 'dbname': 'postgres', 'user': 'postgres', 'password': ''}
            identity = {key: params[key] for key in ('host', 'port', 'dbname', 'user')}
            yield {'params': params, 'data': folder / 'media-target',
                   'fingerprint': fingerprint(json.dumps(identity, sort_keys=True)),
                   'data_fingerprint': fingerprint(str(folder / 'media-target'))}
        finally:
            if started:
                run_pg('pg_ctl', ['-D', str(data), '-m', 'fast', '-w', 'stop'])


def inspect_target(source: Path, target: dict) -> dict:
    manifest = verify_backup(source)
    if target['fingerprint'] == manifest.get('source_fingerprint') or target['data_fingerprint'] == manifest.get('source_data_fingerprint'):
        raise OpsError('不能恢复到原数据库或原媒体目录；请使用独立新目标。')
    with connect(target) as connection:
        tables = counts(connection)
        objects = connection.execute("""SELECT
            (SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace) +
            (SELECT count(*) FROM pg_proc WHERE pronamespace='public'::regnamespace) +
            (SELECT count(*) FROM pg_type WHERE typnamespace='public'::regnamespace)""").fetchone()[0]
        other_clients = connection.execute("""SELECT count(*) FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'""").fetchone()[0]
    if tables:
        raise OpsError('恢复目标数据库必须没有任何业务表；工具不执行clean或覆盖。')
    if objects or other_clients:
        raise OpsError('恢复目标须无公共对象且没有其他客户端；请使用独立空库并保持应用停止。')
    data = target['data']
    if data.is_symlink() or (data.exists() and (not data.is_dir() or any(data.iterdir()))):
        raise OpsError('恢复目标媒体目录必须不存在或为空；不覆盖已有文件。')
    return {'target_fingerprint': target['fingerprint'], 'database_empty': True, 'media_empty': True}


def restore_to(source: Path, target: dict, confirmation: str) -> dict:
    checked = inspect_target(source, target)
    if confirmation != checked['target_fingerprint']:
        raise OpsError('目标确认指纹不匹配，未执行恢复。')
    manifest = verify_backup(source)
    data = target['data']
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    # Stage inside the target filesystem, including a Docker volume mount root.
    with tempfile.TemporaryDirectory(prefix='.shushi-media-stage-', dir=data) as name:
        stage = Path(name) / 'media'
        stage.mkdir(mode=0o700)
        for item in manifest['files']:
            if item['path'] == 'database.dump':
                continue
            relative = PurePosixPath(item['path']).parts[1:]
            copy = stage.joinpath(*relative)
            copy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(manifest_path(source, item['path']), copy)
            copy.chmod(0o600)
            if sha256(copy) != item['sha256']:
                raise OpsError('恢复媒体校验失败；未启动应用。')
        # Fresh databases already contain an empty public schema. Exclude only its
        # creation TOC entry, rather than introducing DROP/--clean to work around it.
        listed = subprocess.run([pg_tool('pg_restore'), '--list', str(source / 'database.dump')],
                                capture_output=True, timeout=30)
        if listed.returncode:
            raise OpsError('归档目录读取失败，未执行恢复。')
        toc = listed.stdout.decode('utf-8')
        filtered = '\n'.join(line for line in toc.splitlines()
                             if not re.match(r'^\d+;\s+\d+\s+\d+\s+SCHEMA\s+-\s+public\s+', line)) + '\n'
        list_file = Path(name) / 'restore.list'
        list_file.write_text(filtered)
        list_file.chmod(0o600)
        run_pg('pg_restore', ['--no-owner', '--no-privileges', '--exit-on-error', '--single-transaction',
                              '--use-list=' + str(list_file), '--dbname=' + target['params']['dbname'],
                              str(source / 'database.dump')], target)
        with connect(target) as connection:
            restored = counts(connection)
        if restored != manifest['database_counts']:
            raise OpsError('恢复后的数据表行数与备份不一致；目标保持离线，请保留备份排查。')
        if any(path != Path(name) for path in data.iterdir()):
            raise OpsError('媒体目标在恢复中发生变化；数据库已恢复，应用仍应保持停止。')
        for child in stage.iterdir():
            os.replace(child, data / child.name)
    return {'operation': 'restore', 'database_counts': restored, 'media_files': len(manifest['files']) - 1,
            'database_verified': True, 'media_verified': True, 'application_started': False}


def dry_run(source: Path) -> dict:
    verify_backup(source)
    with temporary_postgres() as target:
        result = restore_to(source, target, target['fingerprint'])
    return {**result, 'operation': 'isolated_restore_drill', 'real_target_written': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    create = sub.add_parser('backup')
    create.add_argument('output', type=Path)
    create.add_argument('--config-file', type=Path)
    restore = sub.add_parser('restore')
    restore.add_argument('source', type=Path)
    restore.add_argument('--apply', action='store_true')
    restore.add_argument('--dry-run', action='store_true')
    restore.add_argument('--target-config-file', type=Path)
    restore.add_argument('--inspect-target', action='store_true')
    restore.add_argument('--confirm-target')
    args = parser.parse_args()
    try:
        if args.operation == 'backup':
            result = backup(args.output, configuration(args.config_file))
        elif args.apply or args.inspect_target:
            if not args.target_config_file or (args.apply and args.dry_run):
                raise OpsError('真实恢复需指定独立目标配置；apply不能与dry-run同时使用。')
            if any(key in os.environ for key in ('DATABASE_URL', 'DATA_DIR')):
                raise OpsError('请先清除终端DATABASE_URL/DATA_DIR，避免覆盖明确的恢复目标。')
            target = configuration(args.target_config_file, explicit_target=True)
            result = restore_to(args.source, target, args.confirm_target or '') if args.apply else inspect_target(args.source, target)
        else:
            result = dry_run(args.source)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except OpsError as error:
        print(f'操作未完成：{error}', file=sys.stderr)
        return 1
    except Exception:
        print('操作未完成：配置、工具或数据库检查失败；未输出连接信息或原始数据。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
