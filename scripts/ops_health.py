"""Read-only HTTP readiness/auth boundary; optional exact local process count."""
import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

PROJECT = Path(__file__).resolve().parents[1]


def local_backend_count() -> int:
    import psutil
    count = 0
    for process in psutil.process_iter(['cmdline', 'cwd']):
        try:
            command = process.info['cmdline'] or []
            if not any(Path(part).name == 'uvicorn' for part in command) or 'app.main:app' not in command:
                continue
            if '--app-dir' in command:
                index = command.index('--app-dir')
                match = index + 1 < len(command) and Path(command[index + 1]).resolve() == PROJECT / 'backend'
            else:
                match = Path(process.info['cwd'] or '/').resolve() in {PROJECT, PROJECT / 'backend'}
            if match:
                if '--reload' in command or ('--workers' in command and command[command.index('--workers') + 1] != '1'):
                    raise ValueError('multi-worker or reload cannot pass this single-instance check')
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', nargs='?', default='http://127.0.0.1:8000')
    parser.add_argument('--local-process-check', action='store_true', help='Exactly one local project API; not for containers/remote hosts')
    args = parser.parse_args()
    try:
        url = urlsplit(args.base)
        if url.scheme not in {'http', 'https'} or not url.netloc or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}:
            raise ValueError()
        if args.local_process_check and url.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError()
        opener = build_opener(ProxyHandler({}))
        with opener.open(args.base.rstrip('/') + '/api/v1/health', timeout=5) as response:
            health = json.load(response)
        healthy = (health.get('status') == 'ok' and health.get('database') == 'ok'
                   and health.get('environment') in {'local-development', 'production'})
        try:
            response = opener.open(Request(args.base.rstrip('/') + '/api/v1/foods'), timeout=5)
            code = response.status
            response.close()
        except HTTPError as error:
            code = error.code
        report = {'http_and_database': 'ok' if healthy else 'failed',
                  'unauthenticated_foods': 'ok' if code == 401 else 'failed',
                  'single_worker': 'not_inspected__HTTP_does_not_prove_process_count'}
        if args.local_process_check:
            count = local_backend_count()
            report['single_worker'] = 'ok' if count == 1 else 'failed'
            report['local_api_processes'] = count
            healthy = healthy and count == 1
        print(json.dumps(report, ensure_ascii=False))
        return 0 if healthy and code == 401 else 1
    except Exception:
        print('健康检查未通过：地址、HTTP、数据库或鉴权检查失败；未输出响应正文。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
