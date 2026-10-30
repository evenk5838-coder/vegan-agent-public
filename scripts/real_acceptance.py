"""Explicitly authorized upstream smoke checks through the actual API/services.

Uses a separate persistent PostgreSQL cluster and synthetic/public inputs. It is
not browser, mobile, production deployment or public-content publication proof.
No network is sent by --plan. The run is serial, money is reserved before I/O,
and an error stops all later upstream calls (including application retries).
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import secrets
import sys
import threading

PROJECT = Path(__file__).resolve().parents[1]
PLAN = {
    'version': 'real-service-smoke-2026-10-27.1',
    'isolation': 'independent PostgreSQL and synthetic account; no business DB',
    'samples': ['ingredients-three-diets', 'restaurant-city-p1', 'restaurant-city-p2',
                'restaurant-around-public-landmark', 'real-tool-mixed-question',
                'food-draft-no-auto-save', 'empty-knowledge-no-invented-citation',
                'web-recipe-candidates', 'synthetic-ingredient-photo'],
    'excluded': ['private chats/photos/location', 'voice without a supplied public fixture',
                 'push delivery and physical mobile', 'publication', 'cloud resources'],
    'max_cny': 10, 'max_calls': 40, 'automatic_retries': False,
    'rate_sources': ['https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
                     'https://developer.amap.com/upgrade'],
}
PLAN_HASH = hashlib.sha256(json.dumps(PLAN, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


class BudgetStop(RuntimeError):
    pass


class Budget:
    def __init__(self, path: Path, approval: dict):
        if (approval.get('approved') is not True or approval.get('plan_sha256') != PLAN_HASH
                or approval.get('max_cny') != 10 or approval.get('max_calls') != 40
                or not approval.get('authorization_evidence')
                or approval.get('ledger_path') != str(path.resolve())):
            raise BudgetStop('Exact approved plan and caps required')
        expiry = datetime.fromisoformat(approval['expires_at'])
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise BudgetStop('Approval expired')
        if path.exists():
            raise BudgetStop('Existing run is immutable; inspect it, do not reset or repeat requests')
        self.path, self.lock, self.stopped = path, threading.Lock(), False
        self.state = {'plan_sha256': PLAN_HASH, 'max_micros': 10_000_000,
                      'max_calls': 40, 'calls': [], 'prior_calls': 0, 'charged_or_held_micros': 0}
        if approval.get('prior_ledger'):
            previous_path = Path(approval['prior_ledger'])
            previous_bytes = previous_path.read_bytes()
            if hashlib.sha256(previous_bytes).hexdigest() != approval.get('prior_ledger_sha256'):
                raise BudgetStop('Previous receipt changed; aggregate budget cannot be verified')
            previous = json.loads(previous_bytes)
            previous_calls = previous.get('calls')
            previous_cost = previous.get('charged_or_held_micros')
            if (previous.get('plan_sha256') != PLAN_HASH or not isinstance(previous_calls, list)
                    or previous.get('prior_calls', 0) != 0 or type(previous_cost) is not int
                    or previous_cost != sum(c.get('charged_or_held_micros', -1) for c in previous_calls)
                    or not 0 <= previous_cost <= 10_000_000 or len(previous_calls) > 40):
                raise BudgetStop('Previous receipt is invalid or chained; manual reconciliation required')
            self.state.update(prior_calls=len(previous_calls), charged_or_held_micros=previous_cost,
                              prior_ledger={'path': str(previous_path.resolve()),
                                            'sha256': approval['prior_ledger_sha256'],
                                            'calls': len(previous_calls), 'charged_or_held_micros': previous_cost})
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x') as handle:
                handle.write(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')
            path.chmod(0o600)
        except FileExistsError:
            raise BudgetStop('Concurrent or previous run already owns this ledger') from None

    def reserve(self, url, kwargs):
        body = kwargs.get('json') or {}
        if url == 'https://api.deepseek.com/chat/completions':
            if body.get('model') != 'deepseek-flash':
                raise BudgetStop('Model has no checked price in this acceptance plan')
            maximum = body.get('max_tokens')
            if type(maximum) is not int or not 1 <= maximum <= 16384:
                raise BudgetStop('Output token hard cap required')
            reserve = (len(json.dumps(body, ensure_ascii=False).encode()) + 2048) * 2 + maximum * 8
            service = 'deepseek-model'
        elif url == 'https://api.deepseek.com/v1/tools/search':
            service, reserve = 'deepseek-search', 10_000  # 0.01 > listed 0.005/request
        elif url in ('https://restapi.amap.com/v3/place/text', 'https://restapi.amap.com/v3/place/around'):
            service, reserve = 'amap-search', 10_000  # 0.01 > listed 0.003/request
        else:
            raise BudgetStop('Endpoint is outside this plan')
        with self.lock:
            if (self.stopped or self.state['prior_calls'] + len(self.state['calls']) >= 40
                    or self.state['charged_or_held_micros'] + reserve > 10_000_000):
                raise BudgetStop('Stopped or hard call/currency limit reached before network')
            call = {'number': len(self.state['calls']) + 1, 'service': service,
                    'reserved_micros': reserve, 'charged_or_held_micros': reserve,
                    'status': 'reserved', 'started_at': datetime.now(timezone.utc).isoformat()}
            self.state['calls'].append(call)
            self.state['charged_or_held_micros'] += reserve
            write_json(self.path, self.state)
            return call

    def finish(self, call, result=None, failed=False):
        with self.lock:
            call['status'] = 'failed_or_unknown' if failed else 'succeeded'
            self.stopped = self.stopped or failed
            if not failed and call['service'] == 'deepseek-model':
                usage = result.get('usage') or {}
                incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
                if (type(incoming) is int and type(outgoing) is int and incoming >= 0 and outgoing >= 0
                        and incoming + outgoing == usage.get('total_tokens')):
                    peak = incoming * 2 + outgoing * 8
                    if peak <= call['reserved_micros']:
                        self.state['charged_or_held_micros'] += peak - call['charged_or_held_micros']
                        call.update(charged_or_held_micros=peak, usage=usage, settlement='verified_usage_at_peak_rate')
                    else:
                        self.stopped = True
                        call['status'] = 'usage_exceeds_reserved_bound'
                else:
                    self.stopped = True
                    call['status'] = 'usage_unverified'
            if result is not None:
                call['response_sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
            write_json(self.path, self.state)


@contextmanager
def guarded_provider(providers, budget):
    original = providers.request_json

    def checked(method, url, **kwargs):
        try:
            call = budget.reserve(url, kwargs)
        except BudgetStop:
            from app.core import AppError
            raise AppError('ACCEPTANCE_STOPPED', '真实验收已按计划停止，不再外呼。', 429) from None
        try:
            result = original(method, url, **kwargs)
        except Exception:
            budget.finish(call, failed=True)
            raise
        budget.finish(call, result)
        return result

    providers.request_json = checked
    try:
        yield
    finally:
        providers.request_json = original


def run(output: Path, approval_path: Path, only=None):
    # No application import, credential read or network before approval checks.
    approval = json.loads(approval_path.read_text())
    if output.is_relative_to(PROJECT) or output.is_symlink():
        raise BudgetStop('Acceptance output must be a fresh directory outside the application checkout')
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    budget = Budget(output / 'ledger.json', approval)
    sys.path.insert(0, str(PROJECT / 'backend'))
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from app import core, providers, worker
    from app.main import app

    # Never inherit the live database URL. Application account quotas stay as configured.
    core.settings.database_url = ''
    core.settings.data_dir = output / 'isolated-data'
    core.settings.app_environment = 'real-service-acceptance-isolated'
    core.init_db()
    command.upgrade(Config(str(PROJECT / 'backend/alembic.ini')), 'head')

    async def idle_worker():
        await asyncio.Event().wait()

    worker.loop = idle_worker  # Scheduling only. process_once invokes real provider functions below.
    report = {'plan': PLAN, 'plan_sha256': PLAN_HASH, 'results': [],
              'selected_samples': sorted(only) if only else PLAN['samples'],
              'classification': 'real-upstream-isolated-api; NOT browser/full-product acceptance',
              'publication_executed': False, 'business_database_touched': False}

    def check(response, expected=200):
        if response.status_code != expected:
            detail = response.json().get('error', {})
            raise BudgetStop(f'API status {response.status_code}, code {detail.get("code", "unknown")}')
        return response.json()

    def scenario(name, operation):
        if only and name not in only:
            return None
        if budget.stopped:
            raise BudgetStop('Previous upstream error; no later scenario sent')
        result = operation()
        if budget.stopped:
            raise BudgetStop('Upstream receipt failure; stopped')
        report['results'].append({'name': name, 'state': 'passed', 'result': result})
        write_json(output / 'report.json', report)
        print(json.dumps({'scenario': name, 'state': 'passed', 'external_calls': len(budget.state['calls'])}, ensure_ascii=False), flush=True)
        return result

    def ensure(condition, message):
        if not condition:
            raise BudgetStop(message)

    def cards(result):
        return (result.get('reply') or {}).get('cards') or result.get('cards') or []

    try:
        with guarded_provider(providers, budget), TestClient(app) as client:
            check(client.post('/api/v1/auth/register', json={'username': 'acceptance_' + secrets.token_hex(5),
                                                           'password': secrets.token_urlsafe(24)}), 201)
            report['capabilities'] = check(client.get('/api/v1/capabilities'))
            def ingredients():
                result = check(client.post('/api/v1/ingredients/evaluate',
                               json={'text': '水、大豆、白砂糖、洋葱', 'complete': True, 'label_warnings': []}))
                ensure({k: v['status'] for k, v in result['results'].items()} ==
                       {'pure': 'not_suitable', 'vegan': 'suitable', 'lacto_ovo': 'suitable'},
                       'Three independent dietary results differ from deterministic expectation')
                return result
            scenario('ingredients-three-diets', ingredients)
            for name, params in [('restaurant-city-p1', {'city': '北京', 'page': 1, 'page_size': 20}),
                                 ('restaurant-city-p2', {'city': '北京', 'page': 2, 'page_size': 20}),
                                 ('restaurant-around-public-landmark', {'location': '116.3975,39.9087', 'page': 1, 'page_size': 20})]:
                def restaurant(params=params):
                    result = check(client.get('/api/v1/restaurants', params=params))
                    ensure(result.get('scope', {}).get('version') == 'veg-admission-v3', 'Old admission version in live result')
                    ensure(result.get('pagination', {}).get('page') == params['page'], 'Live pagination drifted')
                    known_bad = ('粤知一二隆江猪脚饭', '馅老满', '护国寺小吃', '陕西手擀面·川湘菜',
                                 '麦田归来', '轻易轻食', '一周轻食')
                    ensure(not any(any(word in str(item.get('name', '')) for word in known_bad)
                                   for item in result.get('items', [])), 'Previously observed unsuitable store still admitted')
                    return result
                scenario(name, restaurant)

            def chat(text):
                session = check(client.post('/api/v1/chat/sessions', json={'title': '公开合成样例真实验收'}), 201)['id']
                turn = check(client.post(f'/api/v1/chat/sessions/{session}/messages',
                             json={'content': text, 'idempotency_key': secrets.token_hex(16)}), 201)
                worker.process_once()  # Exactly one task dispatch; no automatic retry loop.
                result = check(client.get(f'/api/v1/chat/sessions/{session}/turns/{turn["turn_id"]}'))
                if result['status'] != 'succeeded':
                    raise BudgetStop('Real chat turn did not succeed: ' + result['status'])
                return result

            def mixed():
                result = chat('请查北京的素食餐厅。另外配料是水、大豆、白砂糖、洋葱，请用规则判断纯净素、Vegan和蛋奶素三类。')
                ensure({'restaurant_list', 'ingredient_result'} <= {c['type'] for c in cards(result)},
                       'Mixed question did not preserve both real tool results')
                return result
            scenario('real-tool-mixed-question', mixed)
            def draft():
                result = chat('今天买了西兰花和豆腐，已放冰箱冷藏。帮我整理成食材草稿，等我确认，不要直接保存。')
                ensure('food_draft' in {c['type'] for c in cards(result)}, 'Missing pending food draft')
                ensure(check(client.get('/api/v1/foods'))['items'] == [], 'Draft unexpectedly wrote formal inventory')
                return result
            scenario('food-draft-no-auto-save', draft)
            def empty_knowledge():
                result = chat('请从已发布的知识库检索隔夜燕麦，给出来源；如果没有依据，请明确说没有，不要联网补位。')
                ensure('knowledge_result' in {c['type'] for c in cards(result)}, 'No real knowledge tool result')
                ensure(not (result.get('reply') or {}).get('citations'), 'Empty source library produced a citation')
                return result
            scenario('empty-knowledge-no-invented-citation', empty_knowledge)
            scenario('web-recipe-candidates', lambda: check(client.post('/api/v1/recipes/search', json={'query': '鹰嘴豆沙拉'})))

            def photo():
                from PIL import Image, ImageDraw, ImageFont
                image = Image.new('RGB', (1100, 350), 'white')
                font = ImageFont.truetype('/System/Library/Fonts/PingFang.ttc', 56)
                ImageDraw.Draw(image).text((45, 110), '配料：水、大豆、白砂糖、洋葱', font=font, fill='black')
                buffer = io.BytesIO()
                image.save(buffer, 'PNG')
                task = check(client.post('/api/v1/ingredients/photo', files={'file': ('synthetic-label.png', buffer.getvalue(), 'image/png')},
                                         data={'save_original': 'false'}), 202)
                worker.process_once()
                result = check(client.get('/api/v1/jobs/' + task['job_id']))
                if result['status'] != 'succeeded':
                    raise BudgetStop('Synthetic photo task did not succeed: ' + result['status'])
                return result
            scenario('synthetic-ingredient-photo', photo)
            report['state'] = 'selected_real_smoke_completed'
    except Exception as exc:
        report['state'] = 'stopped'
        # Only known-safe local exceptions are displayed. Do not dump HTTP/key traces.
        report['reason'] = str(exc) if isinstance(exc, BudgetStop) else type(exc).__name__
        budget.stopped = True
    finally:
        report['budget'] = {'calls_this_run': len(budget.state['calls']),
                            'calls_aggregate': budget.state['prior_calls'] + len(budget.state['calls']),
                            'charged_or_held_micros': budget.state['charged_or_held_micros'],
                            'invoice_reconciled': False}
        write_json(output / 'report.json', report)
        core.engine.dispose()
    print(json.dumps({'state': report['state'], 'reason': report.get('reason'), **report['budget']}, ensure_ascii=False), flush=True)
    return 0 if report['state'] == 'selected_real_smoke_completed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--authorization', type=Path)
    parser.add_argument('--only', nargs='+', choices=PLAN['samples'], help='Repeat only affected checks after a concrete fix; prior ledger still counts')
    args = parser.parse_args()
    if args.plan:
        print(json.dumps({'plan_sha256': PLAN_HASH, **PLAN}, ensure_ascii=False, indent=2))
    elif args.run and args.output and args.authorization:
        raise SystemExit(run(args.output.resolve(), args.authorization, set(args.only or [])))
    else:
        parser.error('Use --plan, or explicitly --run --output --authorization')
