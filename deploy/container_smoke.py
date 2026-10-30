"""Real containers with synthetic data, only on a public GitHub-hosted Linux runner.

Never accepts a production configuration, provider credential, project name or
restore target. The random stack is removed on exit; source/workstation data is
neither mounted nor imported. Logs contain stage names, not HTTP bodies or env.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = 'smoke.shushi.example.com'
PUBLIC_REPO = 'evenk5838-coder/vegan-agent-public'
SECRET_KEYS = ('DEEPSEEK_API_KEY', 'AMAP_API_KEY', 'AMAP_JS_KEY',
               'AMAP_JS_SECURITY_CODE', 'TRANSCRIPTION_API_KEY',
               'VAPID_PRIVATE_KEY', 'VAPID_PUBLIC_KEY', 'VAPID_SUBJECT')


class SmokeError(Exception):
    pass


def guard_runner(environ: dict[str, str], root: Path = ROOT) -> None:
    if (platform.system() != 'Linux' or environ.get('GITHUB_ACTIONS') != 'true'
            or environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or environ.get('RUNNER_OS') != 'Linux'
            or environ.get('GITHUB_REPOSITORY') != PUBLIC_REPO
            or Path(environ.get('GITHUB_WORKSPACE', '/')).resolve() != root.resolve()):
        raise SmokeError('Container smoke is restricted to this public GitHub-hosted Linux checkout.')
    try:
        event = json.loads(Path(environ['GITHUB_EVENT_PATH']).read_text())
        public = (event['repository']['private'] is False
                  and event['repository']['full_name'] == PUBLIC_REPO)
    except (KeyError, OSError, TypeError, ValueError):
        public = False
    if not public:
        raise SmokeError('A verified public-repository event is required.')
    if any((root / name).exists() or (root / name).is_symlink() for name in ('.env', 'data')):
        raise SmokeError('Checkout contains runtime configuration or business data; refusing to run.')
    if any(environ.get(key) for key in (*SECRET_KEYS, 'DATABASE_URL', 'DATA_DIR', 'DOCKER_HOST', 'DOCKER_CONTEXT')):
        raise SmokeError('Inherited credentials, runtime data or a remote Docker endpoint are not accepted.')


def clean_environment(environ: dict[str, str]) -> dict[str, str]:
    return {**{key: environ[key] for key in ('PATH', 'HOME') if key in environ},
            'LC_ALL': 'C.UTF-8', 'COMPOSE_ANSI': 'never', 'BUILDKIT_PROGRESS': 'plain'}


def validate_project(project: str) -> None:
    if not re.fullmatch(r'shushi-ci-[0-9a-f]{24}', project):
        raise SmokeError('Only an internally generated CI project can be used or cleaned up.')


def validate_model(model: dict, project: str, root: Path, caddyfile: Path) -> None:
    """Check resolved Compose resources before any container/volume is created."""
    validate_project(project)
    if model.get('name') != project:
        raise SmokeError('Compose selected a different project.')
    for category in ('volumes', 'networks'):
        for value in model.get(category, {}).values():
            if value.get('external') or not value.get('name', '').startswith(project + '_'):
                raise SmokeError('Compose would use resources outside the temporary CI project.')
    services = model.get('services', {})
    api = services.get('api', {}).get('environment', {})
    if str(api.get('EXTERNAL_CALLS_ENABLED', '')).lower() != 'false' or any(api.get(key) for key in SECRET_KEYS):
        raise SmokeError('Provider calls must remain disabled and unconfigured.')
    for name, service in services.items():
        if service.get('container_name'):
            raise SmokeError('Fixed container names are forbidden in the CI stack.')
        if service.get('build') and Path(service['build']['context']).resolve() != root.resolve():
            raise SmokeError('Build context escaped the code-only checkout.')
        for port in service.get('ports', []):
            if name != 'caddy' or port.get('host_ip') != '127.0.0.1':
                raise SmokeError('CI ports must only expose Caddy on loopback.')
        for mount in service.get('volumes', []):
            if mount.get('type') == 'bind' and not (
                name == 'caddy' and Path(mount['source']).resolve() == caddyfile.resolve()
                and mount['target'] == '/etc/caddy/Caddyfile' and mount.get('read_only')):
                raise SmokeError('Unexpected host bind mount in the CI stack.')


class Stack:
    def __init__(self, folder: Path, environ: dict[str, str]):
        self.folder, self.env = folder, clean_environment(environ)
        self.project = 'shushi-ci-' + secrets.token_hex(12)
        self.owned = False
        self.report: dict[str, object] = {'provider_calls_enabled': False, 'synthetic_data_only': True}
        values = {'SITE_DOMAIN': DOMAIN, 'ACME_EMAIL': 'smoke@example.invalid',
                  'POSTGRES_PASSWORD': secrets.token_hex(32), 'RESTORE_POSTGRES_PASSWORD': secrets.token_hex(32),
                  'RELEASE_TAG': self.project, 'EXTERNAL_CALLS_ENABLED': 'false',
                  **{key: '' for key in SECRET_KEYS}}
        self.envfile = folder / 'runtime.env'
        self.envfile.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        self.envfile.chmod(0o600)
        caddy = (ROOT / 'deploy/Caddyfile').read_text()
        if caddy.count('{$SITE_DOMAIN} {') != 1:
            raise SmokeError('Unexpected Caddy site format; the CI TLS override was not applied.')
        self.caddyfile = folder / 'Caddyfile'
        self.caddyfile.write_text(caddy.replace('{$SITE_DOMAIN} {', '{$SITE_DOMAIN} {\n    tls internal', 1))
        self.overlay = folder / 'compose.smoke.yaml'
        self.overlay.write_text('services:\n  caddy:\n    ports: !override\n'
                                '      - "127.0.0.1:18080:80"\n      - "127.0.0.1:18443:443"\n'
                                '    volumes:\n      - ' + json.dumps(str(self.caddyfile) + ':/etc/caddy/Caddyfile:ro') + '\n')
        self.prefix = ['docker', 'compose', '--project-name', self.project,
                       '--env-file', str(self.envfile), '-f', str(ROOT / 'deploy/compose.yaml'),
                       '-f', str(self.overlay)]

    def command(self, args: list[str], *, timeout: int = 180, input_data: bytes | None = None,
                streamed: bool = False) -> bytes:
        result = subprocess.run(args, cwd=ROOT, env=self.env, input=input_data,
                                capture_output=not streamed, timeout=timeout)
        if result.returncode:
            raise SmokeError(f'Command failed during the current stage (exit {result.returncode}); no runtime values were logged.')
        return result.stdout or b''

    def compose(self, *args: str, **kwargs) -> bytes:
        validate_project(self.project)
        return self.command([*self.prefix, *args], **kwargs)

    def stage(self, name: str) -> None:
        print('Container smoke: ' + name, flush=True)

    def request(self, path: str, expected: int, *, account: str = '', body: dict | None = None) -> bytes:
        if not path.startswith('/') or path.startswith('//'):
            raise SmokeError('Only local test paths are supported.')
        args = ['curl', '--silent', '--show-error', '--max-time', '20', '--noproxy', '*',
                '--cacert', str(self.folder / 'ca.crt'),
                '--resolve', DOMAIN + ':443:127.0.0.1',
                '--connect-to', DOMAIN + ':443:127.0.0.1:18443',
                '--write-out', '\n%{http_code}']
        cookie = self.folder / (account + '.cookies')
        if account:
            args += ['--cookie-jar', str(cookie)]
            if cookie.exists():
                args += ['--cookie', str(cookie)]
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            args += ['--header', 'Content-Type: application/json', '--header', 'Origin: https://' + DOMAIN,
                     '--data-binary', '@-']
        raw = self.command([*args, 'https://' + DOMAIN + path], input_data=payload)
        if cookie.exists():
            cookie.chmod(0o600)
        content, status = raw.rsplit(b'\n', 1)
        if status.decode() != str(expected):
            raise SmokeError(f'HTTP contract failed for {path}: expected {expected}, received {status.decode()}.')
        return content

    def execute(self) -> dict:
        self.stage('validate Compose and production Caddy configuration')
        version = self.command(['docker', 'compose', 'version', '--short']).decode().strip().lstrip('v')
        numbers = tuple(int(value) for value in re.findall(r'\d+', version)[:3])
        if numbers < (2, 24, 4):
            raise SmokeError('Docker Compose 2.24.4 or newer is required for isolated port overrides.')
        model = json.loads(self.compose('--profile', 'ops', '--profile', 'restore', 'config', '--format', 'json'))
        validate_model(model, self.project, ROOT, self.caddyfile)
        self.owned = True
        self.compose('pull', 'db', 'caddy', timeout=300, streamed=True)
        self.command(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'caddy',
                      '-e', 'SITE_DOMAIN=' + DOMAIN, '-e', 'ACME_EMAIL=smoke@example.invalid',
                      '-v', str(ROOT / 'deploy/Caddyfile') + ':/etc/caddy/Caddyfile:ro',
                      'caddy:2.10-alpine', 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'])
        self.report['compose_and_original_caddy_validation'] = 'passed'
        self.stage('build fresh API, Web and maintenance images')
        self.compose('--profile', 'ops', 'build', 'api', 'web', 'ops-backup', timeout=1200, streamed=True)
        self.report['linux_container_build'] = 'passed'
        self.stage('start private database, production API, Web and internal-TLS Caddy')
        self.compose('up', '-d', '--wait', '--wait-timeout', '180', 'db', 'api', 'web', 'caddy', timeout=240)
        for _ in range(20):
            try:
                certificate = self.compose('exec', '-T', 'caddy', 'cat', '/data/caddy/pki/authorities/local/root.crt')
                if b'BEGIN CERTIFICATE' in certificate:
                    (self.folder / 'ca.crt').write_bytes(certificate)
                    break
            except SmokeError:
                pass
            time.sleep(1)
        health = json.loads(self.request('/api/v1/health', 200))
        if health != {'status': 'ok', 'database': 'ok', 'environment': 'production'}:
            raise SmokeError('Production readiness did not include a ready database.')
        self.request('/', 200)
        for path in ('/.env', '/.git/config', '/data/anything', '/images/anything'):
            self.request(path, 404)
        self.request('/api/v1/foods', 401)
        self.report['https_proxy_private_paths_and_readiness'] = 'passed'
        self.stage('check synthetic accounts, food inventory and isolation')
        for account in ('alice', 'bob'):
            self.request('/api/v1/auth/register', 201, account=account,
                         body={'username': 'smoke_' + account, 'password': secrets.token_hex(20)})
        food = json.loads(self.request('/api/v1/foods', 201, account='alice', body={'name': 'CI合成豆腐'}))
        identifier = food['id']
        self.check_inventory(identifier)
        self.report['accounts_inventory_and_isolation'] = 'passed'
        self.compose('exec', '-T', 'api', 'python', '-c',
                     "from pathlib import Path; p=Path('/srv/data/images'); p.mkdir(exist_ok=True); "
                     "(p/'smoke.txt').write_bytes(b'CI synthetic media only')")
        self.stage('recreate API and verify persisted inventory')
        self.compose('up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '120', 'api', timeout=180)
        self.check_inventory(identifier)
        self.report['api_recreation_persistence'] = 'passed'
        self.stage('stop writes, back up and perform real isolated restore drill')
        self.compose('stop', 'api')
        backup = json.loads(self.compose('--profile', 'ops', 'run', '--rm', 'ops-backup', 'backup', 'synthetic-ci', timeout=240))
        drill = json.loads(self.compose('--profile', 'ops', 'run', '--rm', 'ops-backup', 'drill', 'synthetic-ci', timeout=240))
        if not drill.get('database_verified') or not drill.get('media_verified') or drill.get('media_files') != 1:
            raise SmokeError('The backup restore drill did not verify both database and synthetic media.')
        self.report['backup_and_real_restore_drill'] = 'passed'
        self.report['restored_media_files'] = drill['media_files']
        self.report['backed_up_tables'] = len(backup.get('database_counts', {}))
        self.compose('start', 'api')
        for attempt in range(30):
            try:
                self.check_inventory(identifier)
                break
            except SmokeError:
                if attempt == 29:
                    raise
                time.sleep(2)
        self.report['source_inventory_after_backup'] = 'passed'
        self.report['limits'] = ['Synthetic internal CA, not public DNS/ACME', 'No vendor calls or phone notification test',
                                 'No production deployment or capacity guarantee']
        return self.report

    def check_inventory(self, identifier: str) -> None:
        alice = json.loads(self.request('/api/v1/foods', 200, account='alice'))
        bob = json.loads(self.request('/api/v1/foods', 200, account='bob'))
        if identifier not in {item['id'] for item in alice['items']} or bob['items']:
            raise SmokeError('Persisted inventory or account isolation failed.')

    def cleanup(self) -> None:
        if self.owned:
            # No caller-supplied project name, external volumes or production env.
            validate_project(self.project)
            self.compose('--profile', 'ops', '--profile', 'restore', 'down', '--volumes', '--remove-orphans', timeout=120)
            self.owned = False


def main() -> int:
    stack = None
    code = 1
    try:
        guard_runner(dict(os.environ))
        with tempfile.TemporaryDirectory(prefix='shushi-ci-config-') as name:
            stack = Stack(Path(name), dict(os.environ))
            try:
                report = stack.execute()
                code = 0
            finally:
                stack.cleanup()
            print(json.dumps(report, ensure_ascii=False))
    except SmokeError as error:
        print(str(error), file=sys.stderr)
        code = 1
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        print('Container smoke did not complete; no configuration values or HTTP bodies were logged.', file=sys.stderr)
        code = 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())
