"""Collect openly licensed Cookbook source text, without keys or business DB.

Uses audited page IDs; resumes from per-page JSON. Source categories are metadata,
not dietary decisions. No images are downloaded and no content is published.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = 'https://en.wikibooks.org/w/api.php'
USER_AGENT = 'ShushiRecipeResearch/0.1 (open Cookbook text; local noncommercial prototype)'


class RateLimited(RuntimeError):
    pass


def request(params: dict) -> dict:
    url = API + '?' + urllib.parse.urlencode({'format': 'json', 'formatversion': '2', 'maxlag': '5', **params})
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as response:
                data = json.load(response)
            if 'error' in data:
                raise RuntimeError(str(data['error'].get('code')))
            return data
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise RateLimited('HTTP 429; stop collection and honor Retry-After=' + str(error.headers.get('Retry-After', 'unspecified'))) from error
            if attempt == 2:
                raise RuntimeError('HTTP ' + str(error.code)) from error
            time.sleep(2 * (attempt + 1))
        except (OSError, ValueError, RuntimeError) as error:
            if attempt == 2:
                raise RuntimeError(type(error).__name__ + ': ' + str(error)[:150]) from error
            time.sleep(2 * (attempt + 1))
    raise AssertionError('unreachable')


def collect(audit: Path, output: Path, limit: int | None = None) -> dict:
    data = json.loads(audit.read_text())['wikibooks']
    diet_ids = {row['pageid'] for key in ('Vegan recipes', 'Vegetarian recipes')
                for row in data[key]['pages'] if row['ns'] == 102}
    rows = [row for row in data['Recipes']['pages'] if row['ns'] == 102]
    rows.sort(key=lambda row: (row['pageid'] not in diet_ids, row['title']))
    if limit:
        rows = rows[:limit]
    output.mkdir(parents=True, exist_ok=True)
    missing = [row for row in rows if not (output / f"{row['pageid']}.json").is_file()]
    errors = []
    requests = 0
    for offset in range(0, len(missing), 50):
        batch = missing[offset:offset + 50]
        try:
            response = request({'action': 'query', 'prop': 'revisions|categories',
                                'pageids': '|'.join(str(row['pageid']) for row in batch),
                                'rvprop': 'ids|timestamp|sha1|content', 'rvslots': 'main',
                                'cllimit': 'max'})
            requests += 1
            returned = set()
            for page in response.get('query', {}).get('pages', []):
                page_id = page.get('pageid')
                if page_id is None:
                    continue
                returned.add(page_id)
                page['retrieved_at'] = datetime.now(timezone.utc).isoformat()
                page['license'] = {'id': 'CC-BY-SA-4.0',
                                   'url': 'https://creativecommons.org/licenses/by-sa/4.0/',
                                   'attribution': 'Wikibooks contributors',
                                   'policy_url': 'https://en.wikibooks.org/wiki/Wikibooks:Copyrights'}
                page['source_category_hints_only'] = True
                target = output / f'{page_id}.json'
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps(page, ensure_ascii=False, indent=2) + '\n')
                temporary.replace(target)
            errors.extend({'pageid': row['pageid'], 'reason': 'missing_response'}
                          for row in batch if row['pageid'] not in returned)
        except RateLimited as error:
            errors.extend({'pageid': row['pageid'], 'reason': str(error)} for row in missing[offset:])
            break
        except RuntimeError as error:
            errors.extend({'pageid': row['pageid'], 'reason': str(error)} for row in batch)
        print(json.dumps({'batch_done': min(offset + 50, len(missing)),
                          'pending_total': len(missing), 'errors': len(errors)}), flush=True)
        time.sleep(0.7)
    manifest = {'source': API, 'text_only': True, 'target_pages': len(rows),
                'cached_pages': sum((output / f"{row['pageid']}.json").is_file() for row in rows),
                'successful_batch_requests_this_run': requests, 'errors': errors,
                'audit_sha256': hashlib.sha256(audit.read_bytes()).hexdigest(),
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'publication': 'none; raw text is not a released recipe library'}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit', type=Path, default=ROOT / 'docs/知识库/大批量建库-2026-10-26/source-audit.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/recipe-sources/wikibooks')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    result = collect(args.audit, args.output, args.limit)
    print(json.dumps({**{k: v for k, v in result.items() if k != 'errors'}, 'error_count': len(result['errors'])}, ensure_ascii=False))
