"""Private, no-provider readiness request. Nonzero on missing DB readiness evidence."""
import json
from urllib.request import ProxyHandler, build_opener


def main() -> int:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open('http://127.0.0.1:8000/api/v1/health', timeout=3) as response:
            data = json.load(response)
        return 0 if (data.get('status') == 'ok' and data.get('database') == 'ok'
                     and data.get('environment') == 'production') else 1
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
