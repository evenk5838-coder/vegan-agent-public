"""Offline production preflight. Never imports app/core or prints configuration values.

Default mode checks only tracked deployment/source files. --env-file reads the
explicit private file; it must be outside the checkout and not group/world readable.
No mode starts Docker, contacts a server, obtains certificates or changes data.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import stat
import sys
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = Path(__file__).resolve().parent
KEYS = {
    'SITE_DOMAIN', 'ACME_EMAIL', 'POSTGRES_PASSWORD', 'RESTORE_POSTGRES_PASSWORD', 'RELEASE_TAG', 'EXTERNAL_CALLS_ENABLED',
    'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'AMAP_API_KEY', 'AMAP_JS_KEY', 'AMAP_JS_SECURITY_CODE',
    'TRANSCRIPTION_PROVIDER', 'TRANSCRIPTION_API_KEY', 'TRANSCRIPTION_ENDPOINT', 'TRANSCRIPTION_MODEL',
    'VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY', 'VAPID_SUBJECT',
    'DAILY_EXTERNAL_LIMIT', 'WEEKLY_EXTERNAL_LIMIT', 'GLOBAL_DAILY_EXTERNAL_LIMIT',
    'GLOBAL_WEEKLY_EXTERNAL_LIMIT',
}
LIMITS = ('DAILY_EXTERNAL_LIMIT', 'WEEKLY_EXTERNAL_LIMIT', 'GLOBAL_DAILY_EXTERNAL_LIMIT',
          'GLOBAL_WEEKLY_EXTERNAL_LIMIT')


class PreflightError(Exception):
    pass


def parse_env(text: str) -> dict[str, str]:
    """Restricted Compose-compatible syntax, with no shell eval or value echoing."""
    values = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, equals, value = line.partition('=')
        if not equals or key not in KEYS:
            raise PreflightError(f'运行配置第 {line_number} 行字段不支持。')
        if key in values:
            raise PreflightError(f'运行配置字段 {key} 重复。')
        value = value.strip()
        if value.startswith("'"):
            if not value.endswith("'") or len(value) < 2 or "'" in value[1:-1]:
                raise PreflightError(f'运行配置字段 {key} 的单引号格式不正确。')
            value = value[1:-1]
        elif any(char in value for char in ('"', "'", '$', '#')) or any(c.isspace() for c in value):
            raise PreflightError(f'运行配置字段 {key} 请使用单引号字面值，禁止插值或行内注释。')
        if any(ord(char) < 32 for char in value):
            raise PreflightError(f'运行配置字段 {key} 包含控制字符。')
        values[key] = value
    return values


def validate_runtime(values: dict[str, str], container: bool = False) -> list[str]:
    """Validate relationships, not provider credentials or resource ownership."""
    domain = values.get('SITE_DOMAIN', '')
    if (not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain)
            or domain.endswith(('.example', '.invalid', '.test', '.localhost', '.local', '.internal'))
            or domain in {'example.com', 'example.org', 'example.net'}):
        raise PreflightError('SITE_DOMAIN 必须是已获批准的真实域名，不含协议、端口或路径。')
    email = values.get('ACME_EMAIL', '')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise PreflightError('ACME_EMAIL 需要填写证书通知邮箱。')
    password = values.get('POSTGRES_PASSWORD', '')
    if (len(password) < 32 or any(c.isspace() for c in password)
            or any(token in password.casefold() for token in ('change_me', 'changeme', 'replace_me', 'password'))):
        raise PreflightError('POSTGRES_PASSWORD 需要至少32位的独立随机值，不能使用示例或常用密码。')
    if not container and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', values.get('RELEASE_TAG', '')):
        raise PreflightError('RELEASE_TAG 必须填写本次唯一的镜像发布标识。')
    enabled = values.get('EXTERNAL_CALLS_ENABLED', 'false').lower()
    if enabled not in {'true', 'false'}:
        raise PreflightError('EXTERNAL_CALLS_ENABLED 只能是 true 或 false。')
    for key, fallback in zip(LIMITS, ('10', '30', '30', '100')):
        number = values.get(key, fallback)
        if not number.isdigit() or not 1 <= int(number) <= 100000:
            raise PreflightError(f'{key} 需要是有效的正整数额度。')
    if int(values.get('DAILY_EXTERNAL_LIMIT', '10')) > int(values.get('WEEKLY_EXTERNAL_LIMIT', '30')):
        raise PreflightError('个人每日额度不能大于个人每周额度。')
    if int(values.get('GLOBAL_DAILY_EXTERNAL_LIMIT', '30')) > int(values.get('GLOBAL_WEEKLY_EXTERNAL_LIMIT', '100')):
        raise PreflightError('全项目每日额度不能大于全项目每周额度。')
    if container:
        expected = f'https://{domain}'
        if values.get('APP_ENVIRONMENT') != 'production':
            raise PreflightError('生产 APP_ENVIRONMENT 必须明确设置为 production。')
        if values.get('APP_ORIGIN') != expected or values.get('COOKIE_SECURE', '').lower() != 'true':
            raise PreflightError('生产 APP_ORIGIN 必须与 HTTPS 域名完全一致，COOKIE_SECURE 必须为 true。')
        if values.get('WEB_CONCURRENCY', '1') != '1':
            raise PreflightError('生产API只允许一个 worker。')
        if values.get('DATA_DIR') != '/srv/data':
            raise PreflightError('生产 DATA_DIR 必须指向独立持久卷 /srv/data。')
        if values.get('POSTGRES_HOST') != 'db' or values.get('POSTGRES_PORT', '5432') != '5432':
            raise PreflightError('本部署包只连接私有网络的 db:5432。')
    warnings = []
    if enabled == 'true':
        warnings.append('真实外呼已开启；必须已有本轮预算授权，本检查不授予费用权限。')
    if not values.get('DEEPSEEK_API_KEY'):
        warnings.append('模型凭据为空：对话与识图将保持未配置。')
    if not all(values.get(key) for key in ('AMAP_API_KEY', 'AMAP_JS_KEY', 'AMAP_JS_SECURITY_CODE')):
        warnings.append('高德凭据未齐：餐厅或地图能力可能不可用。')
    if not values.get('TRANSCRIPTION_API_KEY'):
        warnings.append('语音未配置：保留文字输入。')
    if not all(values.get(key) for key in ('VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY', 'VAPID_SUBJECT')):
        warnings.append('推送未配置：不能当作真机推送已通过。')
    return warnings


def read_private_env(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise PreflightError('运行配置不能是符号链接。')
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError:
        raise PreflightError('无法读取指定的运行配置文件。') from None
    if resolved.is_relative_to(ROOT) or not stat.S_ISREG(info.st_mode):
        raise PreflightError('运行配置必须是项目目录以外的普通私有文件。')
    if info.st_mode & 0o077:
        raise PreflightError('运行配置权限过宽，请由所有者设为600。')
    values = parse_env(resolved.read_text(encoding='utf-8'))
    for key, value in values.items():
        if key in os.environ and os.environ[key] != value:
            raise PreflightError(f'当前终端的 {key} 会覆盖运行配置，请先清除该覆盖。')
    return values


def validate_restore_profile(values: dict[str, str]) -> None:
    password = values.get('RESTORE_POSTGRES_PASSWORD', '')
    if len(password) < 32 or any(c.isspace() for c in password) or password == values.get('POSTGRES_PASSWORD'):
        raise PreflightError('restore profile需要另一份至少32位的随机数据库密码，不能复用源库密码。')


def check_files() -> dict:
    required = ('Dockerfile.api', 'Dockerfile.web', 'compose.yaml', 'Caddyfile',
                'runtime.env.example', 'requirements-runtime.txt', 'start_api.py',
                'check_api.py', 'README.md', '.gitignore', 'Dockerfile.ops', 'requirements-ops.txt', 'ops_entry.py')
    for name in required:
        if not (DEPLOY / name).is_file():
            raise PreflightError(f'部署文件缺少 {name}。')
    compose = (DEPLOY / 'compose.yaml').read_text()
    dockerignore = (ROOT / '.dockerignore').read_text()
    for pattern in ('**/.env', '**/.git/**', '**/runtime.env', '**/*.key'):
        if pattern not in dockerignore.splitlines():
            raise PreflightError('Docker构建上下文缺少敏感文件排除。')
    for name in ('Dockerfile.api', 'Dockerfile.web', 'Dockerfile.ops'):
        source = (DEPLOY / name).read_text()
        if re.search(r'^\s*(?:ADD|COPY)\s+\.', source, re.M) or re.search(r'^(?:ARG|ENV)\s+.*(?:API_KEY|PASSWORD|VAPID|NEXT_PUBLIC)', source, re.M):
            raise PreflightError(f'{name} 有全目录复制或凭据构建输入。')
    if compose.count('    ports:') != 1 or any(value not in compose for value in ('"80:80"', '"443:443"', 'replicas: 1', 'internal: true')):
        raise PreflightError('Compose对外端口、单API实例或私有网络约束不正确。')
    locked = set((ROOT / 'backend/requirements.lock.txt').read_text().splitlines())
    for requirements in ('requirements-runtime.txt', 'requirements-ops.txt'):
        runtime = [line for line in (DEPLOY / requirements).read_text().splitlines() if line and not line.startswith('#')]
        if any(line not in locked for line in runtime) or any(line.startswith(('pgserver==', 'pytest==')) for line in runtime):
            raise PreflightError('生产依赖与现有锁文件不一致，或带入开发数据库/测试框架。')
    for name in ('preflight.py', 'start_api.py', 'check_api.py', 'ops_entry.py'):
        ast.parse((DEPLOY / name).read_text(), filename=name)
    template = parse_env((DEPLOY / 'runtime.env.example').read_text())
    if template.get('EXTERNAL_CALLS_ENABLED') != 'false' or any(template.get(key) for key in
        ('POSTGRES_PASSWORD', 'DEEPSEEK_API_KEY', 'AMAP_API_KEY', 'AMAP_JS_SECURITY_CODE', 'VAPID_PRIVATE_KEY')):
        raise PreflightError('示例含凭据或默认开启外呼。')
    return {'source_files': 'ok', 'docker_static_guards': 'ok', 'runtime_tested': False,
            'container_build_tested': False, 'cloud_deployed': False}


class SelfTests(unittest.TestCase):
    @staticmethod
    def valid():
        return {'SITE_DOMAIN': 'shushi.product.cn', 'ACME_EMAIL': 'ops@product.cn',
                'POSTGRES_PASSWORD': 'SYNTHETIC_NotARealSecret_9a72b4d8c0e6',
                'RELEASE_TAG': 'synthetic-test-only', 'EXTERNAL_CALLS_ENABLED': 'false'}

    def test_values_are_not_executed_and_single_quotes_are_literal(self):
        self.assertEqual(parse_env("DEEPSEEK_API_KEY='$(do-not-run)#literal'\n")['DEEPSEEK_API_KEY'], '$(do-not-run)#literal')
        with self.assertRaises(PreflightError):
            parse_env('DEEPSEEK_API_KEY=${EXPAND}\n')

    def test_duplicate_or_unknown_fields_fail_without_echoing_secret(self):
        for text in ('TOKEN=do-not-log', 'DEEPSEEK_API_KEY=do-not-log\nDEEPSEEK_API_KEY=second', 'DEEPSEEK_API_KEY="do-not-log"'):
            with self.assertRaises(PreflightError) as error:
                parse_env(text)
            self.assertNotIn('do-not-log', str(error.exception))

    def test_empty_template_cannot_pass_runtime_validation(self):
        with self.assertRaises(PreflightError):
            validate_runtime(parse_env((DEPLOY / 'runtime.env.example').read_text()))

    def test_domain_cannot_be_url_localhost_or_unapproved_placeholder(self):
        for domain in ('http://shushi.product.cn', '127.0.0.1', 'localhost', 'example.org', 'shushi.product.cn/path'):
            with self.assertRaises(PreflightError):
                validate_runtime({**self.valid(), 'SITE_DOMAIN': domain})

    def test_private_password_does_not_appear_in_validation_error(self):
        for password in ('short', 'password_is_not_random_12345678901234'):
            with self.assertRaises(PreflightError) as error:
                validate_runtime({**self.valid(), 'POSTGRES_PASSWORD': password})
            self.assertNotIn(password, str(error.exception))

    def test_container_requires_exact_origin_secure_cookie_and_one_worker(self):
        values = {**self.valid(), 'APP_ORIGIN': 'https://shushi.product.cn', 'COOKIE_SECURE': 'true',
                  'APP_ENVIRONMENT': 'production', 'WEB_CONCURRENCY': '1', 'DATA_DIR': '/srv/data', 'POSTGRES_HOST': 'db'}
        validate_runtime(values, container=True)
        for change in ({'COOKIE_SECURE': 'false'}, {'APP_ORIGIN': 'https://other.product.cn'}, {'WEB_CONCURRENCY': '2'}, {'DATA_DIR': '/tmp'}):
            with self.assertRaises(PreflightError):
                validate_runtime({**values, **change}, container=True)

    def test_restore_profile_cannot_fall_back_to_source_password(self):
        for password in ('', self.valid()['POSTGRES_PASSWORD']):
            with self.assertRaises(PreflightError):
                validate_restore_profile({**self.valid(), 'RESTORE_POSTGRES_PASSWORD': password})
        validate_restore_profile({**self.valid(), 'RESTORE_POSTGRES_PASSWORD': 'ANOTHER_SyntheticRestoreValue_96a65c02'})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Explicit private runtime file outside the checkout; values never printed')
    parser.add_argument('--self-test', action='store_true', help='Offline synthetic validation tests; no Docker or network')
    parser.add_argument('--restore-profile', action='store_true', help='Validate the separately authorized independent restore profile')
    args = parser.parse_args()
    try:
        report = check_files()
        if args.self_test:
            result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests))
            if not result.wasSuccessful():
                return 1
            report['synthetic_checks'] = result.testsRun
        if args.env_file:
            values = read_private_env(args.env_file)
            report['runtime_configuration'] = 'syntax_and_relationships_only'
            report['warnings'] = validate_runtime(values)
            if args.restore_profile:
                validate_restore_profile(values)
                report['restore_configuration'] = 'independent_password_checked'
        elif args.restore_profile:
            raise PreflightError('restore profile预检需明确指定私有运行配置。')
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (PreflightError, OSError, UnicodeError) as error:
        print(f'预检未通过：{error if isinstance(error, PreflightError) else "部署文件暂不可读取。"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
