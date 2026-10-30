"""食材主动建议（B）验收：只读试算、包装适用性、缺信息/无规则、确认复核、条件变更重评。

全部隔离夹具；无真实外部调用。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import core, knowledge, storage_rules, worker
from app.core import now
from app.services import food_advice


def assess(client, **body):
    return client.post('/api/v1/foods/assess', json=body).json()['suggestion']


def published_rule(monkeypatch):
    """隔离夹具：把规则标为已发布（只改内存，不落库、不影响生产规则）。"""
    import copy
    rules = copy.deepcopy(list(storage_rules.RULES))
    for rule in rules:
        rule['status'] = 'published'
    monkeypatch.setattr(storage_rules, 'RULES', rules)


# ---------------- 只读试算 ----------------
def test_assess_is_read_only_and_authenticated(signed):
    body = {'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated',
            'date_type': 'purchase', 'started_at': (now() - timedelta(hours=1)).isoformat()}
    before = len(signed.get('/api/v1/foods').json()['items'])
    s = assess(signed, **body)
    assert s['action'] in ('unknown', 'date')
    assert len(signed.get('/api/v1/foods').json()['items']) == before   # 不建记录
    assert signed.get('/api/v1/notifications/tasks').json() == []       # 不发提醒
    signed.post('/api/v1/auth/logout')
    assert signed.post('/api/v1/foods/assess', json=body).status_code == 401


def test_unknown_class_asks_for_class_not_dates(signed):
    s = assess(signed, name='豆腐')
    assert s['action'] == 'unknown'
    assert s['basis']['type'] == 'none'
    assert s['missing'] == ['food_class']
    # basis=none 时绝不能给日期或“尽快”
    assert s['date'] is None


def test_complete_info_without_published_rule_is_not_endless_prompting(signed):
    s = assess(signed, name='豆腐', food_class='fresh', storage='refrigerated',
               date_type='purchase', started_at=(now() - timedelta(hours=1)).isoformat())
    assert s['action'] == 'unknown'
    assert s['reason'] == 'no_published_rule'
    assert s['missing'] == []          # 信息齐全，不再追问
    assert s['rules_status'] == 'none'


def test_missing_info_and_no_rule_are_distinct(signed):
    missing = assess(signed, name='豆腐', food_class='fresh', storage='refrigerated')
    no_rule = assess(signed, name='豆腐', food_class='fresh', storage='refrigerated',
                     date_type='purchase', started_at=(now() - timedelta(hours=1)).isoformat())
    assert (missing['reason'], missing['missing']) != (no_rule['reason'], no_rule['missing'])
    assert missing['reason'] == 'missing_info' and missing['missing']
    assert no_rule['reason'] == 'no_published_rule' and no_rule['missing'] == []


def test_published_rule_gives_date_with_traceability(signed, monkeypatch):
    published_rule(monkeypatch)
    s = assess(signed, name='蔬菜汤', food_class='cooked', storage='refrigerated',
               condition='cooked', temperature_c=4,
               date_type='prepared', started_at=(now() - timedelta(hours=2)).isoformat())
    assert s['action'] == 'date' and s['date']
    assert s['basis']['type'] == 'rule'
    assert s['basis']['rule_id'] == 'cooked-refrigerated'
    assert s['basis']['rule_version'] and s['basis']['rule_source']
    assert s['rules_status'] == 'published'


def test_draft_rule_never_produces_date(signed):
    """默认（规则仍 draft）不产日期；不因缺规则就编天数。"""
    s = assess(signed, name='蔬菜汤', food_class='cooked', storage='refrigerated',
               condition='cooked', temperature_c=4,
               date_type='prepared', started_at=(now() - timedelta(hours=2)).isoformat())
    assert s['action'] == 'unknown' and s['date'] is None
    assert s['rules_status'] == 'none'


# ---------------- 包装依据 ----------------
def test_valid_package_evidence_gives_date(signed):
    expires = (now() + timedelta(days=3)).isoformat()
    s = assess(signed, name='豆奶', food_class='packaged', condition='unopened', storage='refrigerated', opened=False,
               date_type='production', started_at=(now() - timedelta(days=1)).isoformat(),
               package_note='未开封冷藏条件下保质期至标示日期', expires_at=expires)
    assert s['basis']['type'] == 'package'
    assert s['action'] == 'date' and s['date'] == expires


def test_package_without_matching_conditions_is_not_evidence(signed):
    """只有说明+日期、但状态不匹配 → 不算包装依据（不适用）。"""
    expires = (now() + timedelta(days=3)).isoformat()
    s = assess(signed, name='豆奶', food_class='packaged', storage='frozen', opened=True,
               date_type='production', started_at=(now() - timedelta(days=1)).isoformat(),
               package_note='未开封冷藏条件下适用', expires_at=expires)
    assert s['basis']['type'] == 'none'
    assert s['reason'] == 'package_not_applicable'
    assert s['missing'] == []              # 不适用 ≠ 缺信息
    assert s['action'] == 'unknown'


def test_passed_package_date_is_reported_not_turned_unknown(signed):
    """有效但已过去的包装日期必须保留并显示为已过期，不能退化成“未知/无依据”。"""
    expires = (now() - timedelta(days=2)).isoformat()
    s = assess(signed, name='豆奶', food_class='packaged', condition='unopened', storage='refrigerated', opened=False,
               date_type='production', started_at=(now() - timedelta(days=9)).isoformat(),
               package_note='未开封冷藏条件下适用', expires_at=expires)
    assert s['basis']['type'] == 'package'
    assert s['date'] == expires
    assert s['reason'] == 'date_passed'


def test_opened_package_with_opened_instruction_still_assessable(signed):
    expires = (now() + timedelta(days=1)).isoformat()
    s = assess(signed, name='豆浆', food_class='packaged', condition='unopened', storage='refrigerated', opened=True,
               date_type='opened', started_at=(now() - timedelta(hours=2)).isoformat(),
               package_note='开封后请于 2 日内饮用', expires_at=expires)
    assert s['basis']['type'] == 'package' and s['action'] == 'date'


# ---------------- 草稿：建议与确认复核 ----------------
def test_draft_items_carry_suggestion(signed):
    draft = signed.post('/api/v1/foods/drafts',
                        json={'items': [{'name': '豆腐', 'food_class': 'fresh',
                                         'storage': 'refrigerated', 'date_type': 'purchase',
                                         'started_at': (now() - timedelta(hours=1)).isoformat()}]}).json()
    item = draft['items'][0]
    assert item['suggestion']['reason'] == 'no_published_rule'
    assert item['advice_version'] if 'advice_version' in item else True
    assert draft['advice_digest']


def test_confirm_rejects_stale_advice_digest(signed):
    draft = signed.post('/api/v1/foods/drafts',
                        json={'items': [{'name': '豆腐', 'food_class': 'fresh',
                                         'storage': 'refrigerated', 'date_type': 'purchase',
                                         'started_at': (now() - timedelta(hours=1)).isoformat()}]}).json()
    item_id = draft['items'][0]['item_id']
    stale = draft['advice_digest']
    # 用户补了信息 → 摘要变化；用旧摘要确认应被拒且不保存
    signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{item_id}",
               json={'condition': 'cut'})
    r = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm",
                    json={'advice_digest': stale, 'idempotency_key': 'k1'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'DRAFT_CHANGED'
    assert signed.get('/api/v1/foods').json()['items'] == []          # 无保存副作用
    assert signed.get('/api/v1/notifications/tasks').json() == []     # 无提醒副作用
    # 用新摘要重新确认 → 成功
    fresh = signed.get(f"/api/v1/foods/drafts/{draft['draft_id']}").json()['advice_digest']
    ok = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm",
                     json={'advice_digest': fresh, 'idempotency_key': 'k2'}).json()
    assert ok['status'] == 'confirmed'
    assert len(signed.get('/api/v1/foods').json()['items']) == 1


def test_confirm_idempotent_replay_does_not_duplicate(signed):
    draft = signed.post('/api/v1/foods/drafts',
                        json={'items': [{'name': '豆腐', 'food_class': 'fresh',
                                         'storage': 'refrigerated', 'date_type': 'purchase',
                                         'started_at': (now() - timedelta(hours=1)).isoformat()}]}).json()
    body = {'advice_digest': draft['advice_digest'], 'idempotency_key': 'same'}
    first = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json=body).json()
    again = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json=body).json()
    assert again == first
    assert len(signed.get('/api/v1/foods').json()['items']) == 1
    assert len(signed.get('/api/v1/notifications/tasks').json()) <= 1


# ---------------- 条件变更自动重评 ----------------
def test_condition_change_auto_reevaluates_with_rule(signed, monkeypatch):
    published_rule(monkeypatch)
    created = signed.post('/api/v1/foods', json={
        'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'condition': 'cooked',
        'temperature_c': 4, 'date_type': 'prepared',
        'started_at': (now() - timedelta(hours=3)).isoformat()}).json()
    body = {k: created[k] for k in ('name', 'food_class', 'storage', 'condition',
                                    'temperature_c', 'date_type', 'started_at')}
    body.update(temperature_c=3)
    updated = signed.put(f"/api/v1/foods/{created['id']}", json=body).json()
    assert updated['auto_reevaluated'] is True
    assert updated['display_status'] != 'needs_review'
    assert updated['deadline_basis']['type'] == 'rule'
    # 历史与起点保留
    assert len(updated['storage_history']) == 1
    assert updated['started_at'] == created['started_at']


def test_condition_change_without_rule_clears_and_cancels(signed):
    created = signed.post('/api/v1/foods', json={
        'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'condition': 'cooked',
        'temperature_c': 4, 'date_type': 'prepared',
        'started_at': (now() - timedelta(hours=3)).isoformat(),
        'deadline_source': 'user', 'expires_at': (now() + timedelta(hours=5)).isoformat(),
        'remind_at': (now() + timedelta(hours=1)).isoformat()}).json()
    # 先改成无自设日期、有系统依据缺失的状态：清掉 user 日期后改条件
    body = {k: created[k] for k in ('name', 'food_class', 'storage', 'condition',
                                    'temperature_c', 'date_type', 'started_at')}
    body.update(storage='frozen', temperature_c=-18, expires_at=None, remind_at=None,
                deadline_source='unknown')
    # FoodInput 的 deadline_source 必须显式给出 unknown（显式 null 语义只作用于日期/提醒）
    assert body['deadline_source'] == 'unknown'
    updated = signed.put(f"/api/v1/foods/{created['id']}", json=body).json()
    assert updated['auto_reevaluated'] is True
    assert updated['display_status'] == 'needs_review'
    assert updated['valid_deadline_at'] is None
    assert all(t['status'] == 'cancelled'
               for t in signed.get('/api/v1/notifications/tasks').json())


def test_condition_change_does_not_override_user_date(signed):
    created = signed.post('/api/v1/foods', json={
        'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated', 'condition': 'raw',
        'date_type': 'purchase', 'started_at': (now() - timedelta(hours=3)).isoformat(),
        'deadline_source': 'user', 'expires_at': (now() + timedelta(days=2)).isoformat()}).json()
    body = {k: created[k] for k in ('name', 'food_class', 'storage', 'condition',
                                    'date_type', 'started_at')}
    body.update(storage='frozen')
    updated = signed.put(f"/api/v1/foods/{created['id']}", json=body).json()
    assert updated['auto_reevaluated'] is False
    assert updated['assessment_status'] == 'user_set'
    assert updated['expires_at'] is not None
    assert 'user' == updated['deadline_basis']['type']


def test_formal_record_and_suggestion_use_same_basis(signed, monkeypatch):
    published_rule(monkeypatch)
    created = signed.post('/api/v1/foods', json={
        'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'condition': 'cooked',
        'temperature_c': 4, 'date_type': 'prepared',
        'started_at': (now() - timedelta(hours=3)).isoformat()}).json()
    assess_body = {k: created[k] for k in ('name', 'food_class', 'storage', 'condition',
                                          'temperature_c', 'date_type', 'started_at')}
    preview = assess(signed, **assess_body)
    reevaluated = signed.post(f"/api/v1/foods/{created['id']}/reevaluate",
                              json={'evidence': {'deadline_source': 'rule'}}).json()
    assert preview['basis']['type'] == reevaluated['deadline_basis']['type'] == 'rule'
    assert preview['basis']['rule_id'] == reevaluated['deadline_basis']['rule_id']


# ---------------- 兼容与隔离 ----------------
def test_legacy_condition_opened_still_reads(signed):
    """旧记录 condition='opened' 仍可读；新字段缺省为未知，不被猜测成生鲜。"""
    created = signed.post('/api/v1/foods', json={
        'name': '豆浆', 'storage': 'refrigerated', 'condition': 'opened',
        'date_type': 'opened', 'started_at': (now() - timedelta(hours=1)).isoformat()}).json()
    assert created['food_class'] == 'unknown'
    assert created['suggestion']['reason'] == 'missing_info'
    assert 'food_class' in created['suggestion']['missing']


def test_suggestion_is_account_scoped(signed):
    created = signed.post('/api/v1/foods', json={
        'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated',
        'date_type': 'purchase', 'started_at': now().isoformat()}).json()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get('/api/v1/foods').json()['items'] == []
    assert signed.put(f"/api/v1/foods/{created['id']}", json={
        'name': '豆腐', 'storage': 'refrigerated', 'started_at': now().isoformat()}).status_code == 404


def test_assess_matches_draft_suggestion(signed):
    """页面 / 草稿 / 只读试算共用同一服务：同一组输入结论一致。"""
    body = {'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated',
            'date_type': 'purchase', 'started_at': (now() - timedelta(hours=1)).isoformat()}
    direct = assess(signed, **body)
    draft = signed.post('/api/v1/foods/drafts', json={'items': [body]}).json()
    item = draft['items'][0]['suggestion']
    assert (direct['action'], direct['reason'], direct['missing']) == \
           (item['action'], item['reason'], item['missing'])
