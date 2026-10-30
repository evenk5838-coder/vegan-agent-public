"""Explicit maintenance commands only; default help performs no data operation."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ops_data as ops


def selected_config(role: str) -> dict:
    from sqlalchemy.engine import URL
    expected = {'backup': ('db', 'shushi', 'shushi', '/source-data'),
                'restore': ('restore-db', 'shushi_restore', 'shushi_restore', '/restore-data')}
    if role not in expected:
        raise ops.OpsError('维护角色未明确设置。')
    host, database, user, data = expected[role]
    password = os.environ.get('POSTGRES_PASSWORD', '')
    if len(password) < 32 or any(c.isspace() for c in password):
        raise ops.OpsError('维护数据库凭据未按私有运行配置准备。')
    # Runtime-only URL. Host/database/user are fixed per profile, not CLI-controlled.
    os.environ['DATABASE_URL'] = URL.create('postgresql+psycopg', username=user, password=password,
                                           host=host, port=5432, database=database).render_as_string(hide_password=False)
    os.environ['DATA_DIR'] = data
    return ops.configuration()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('help', 'backup', 'drill', 'inspect', 'restore'), nargs='?', default='help')
    parser.add_argument('backup_name', nargs='?')
    parser.add_argument('--confirm-target')
    args = parser.parse_args()
    if args.operation == 'help':
        print('维护默认只显示帮助。backup/drill用于ops profile；inspect/restore用于独立restore profile。')
        return 0
    try:
        if not args.backup_name or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', args.backup_name) or '..' in args.backup_name:
            raise ops.OpsError('备份名称只允许普通标识，不接受路径。')
        role = os.environ.get('OPS_ROLE', '')
        required_role = 'backup' if args.operation in {'backup', 'drill'} else 'restore'
        if role != required_role:
            raise ops.OpsError('此维护容器不能执行该角色的数据操作。')
        source = Path('/backups') / args.backup_name
        if args.operation == 'backup':
            report = ops.backup(source, selected_config(role))
        elif args.operation == 'drill':
            report = ops.dry_run(source)
        elif args.operation == 'inspect':
            report = ops.inspect_target(source, selected_config(role))
        else:
            report = ops.restore_to(source, selected_config(role), args.confirm_target or '')
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except ops.OpsError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('维护操作未完成；未输出配置、连接信息或原始数据。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
