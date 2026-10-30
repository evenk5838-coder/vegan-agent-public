"""Reviewed-source scope and date semantics, with isolated draft/publication fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from app import core, storage_rules
from app.services import food_advice, foods


def payload(**changes):
    return {'name': '蔬菜汤', 'food_class': 'cooked', 'condition': 'cooked',
            'storage': 'refrigerated', 'temperature_c': 4.0,
            'location': 'indoor', 'date_type': 'prepared',
            'started_at': (core.now() - timedelta(hours=3)).isoformat(),
            'status': 'active', 'deadline_source': 'unknown', **changes}


def lookup(body, *, published=False, rid='cooked-refrigerated', estimates=True):
    return storage_rules.lookup(body['name'], body['storage'], body['condition'], rid,
        body.get('temperature_c'), body.get('location'), published_only=published,
        food_class=body.get('food_class'), date_type=body.get('date_type'), for_estimates=estimates)


def test_all_three_are_drafts_and_only_one_is_an_expiry_proposal():
    assert len(storage_rules.RULES) == 3
    assert all(rule['status'] == 'draft' for rule in storage_rules.RULES)
    assert sum(storage_rules.can_estimate_expiry(rule) for rule in storage_rules.RULES) == 1
    catalog = storage_rules.describe()
    assert not catalog['usable_for_estimates'] and catalog['estimate_rule_ids'] == []
    fridge, frozen, handling = storage_rules.RULES
    assert fridge['source_days_range'] == (3, 4) and fridge['temperature_c'] == (0.0, 4.0)
    assert frozen['hours'] is None and handling['hours'] is None
    assert frozen['quality_months_range'] == (2, 3)
    assert handling['requires_cumulative_exposure']


@pytest.mark.parametrize('name', ['熟菜', '剩菜', '炒菜', '熟制', '炒青菜', '切黄瓜', '蔬菜汤味饼干'])
def test_does_not_generalize_soup_rule_to_other_food(name):
    rule, reasons = lookup(payload(name=name))
    assert rule is None and '食材类别' in reasons


@pytest.mark.parametrize('changes,reason', [
    ({'food_class': 'fresh'}, '食品类别'), ({'food_class': 'packaged'}, '食品类别'),
    ({'food_class': None}, '食品类别'), ({'condition': 'raw'}, '食材状态'),
    ({'condition': 'cut'}, '食材状态'), ({'storage': 'frozen'}, '存放方式'),
    ({'date_type': 'purchase'}, '日期含义'), ({'date_type': 'production'}, '日期含义'),
    ({'date_type': 'opened'}, '日期含义'), ({'date_type': None}, '日期含义'),
    ({'temperature_c': None}, '温度数值'), ({'temperature_c': 4.1}, '温度范围'),
    ({'temperature_c': 8}, '温度范围'), ({'temperature_c': -1}, '温度范围'),
    ({'temperature_c': float('nan')}, '温度范围'),
])
def test_all_applicability_dimensions_are_required(changes, reason):
    rule, reasons = lookup(payload(**changes))
    assert rule is None and any(reason in value for value in reasons)


@pytest.mark.parametrize('changes', [
    {'started_at': None}, {'started_at': 'yesterday'},
    {'started_at': '2026-10-26T11:00:00'},
    {'started_at': (core.now() + timedelta(hours=1)).isoformat()},
])
def test_missing_invalid_naive_future_preparation_time_cannot_estimate(changes):
    rule, deadline, reasons = storage_rules.estimate_deadline(payload(**changes), published_only=False)
    assert rule is None and deadline is None and reasons


def test_preparation_time_not_current_time_or_storage_change_is_origin(published_rules):
    body = payload()
    rule, deadline, reasons = storage_rules.estimate_deadline(body)
    assert rule and not reasons
    assert deadline == datetime.fromisoformat(body['started_at']) + timedelta(days=4)
    assert rule['date_origin'] == 'preparation_finished_at'
    body['storage_history'] = [{'storage': 'refrigerated', 'temperature_c': 3}]
    assert storage_rules.estimate_deadline(body)[1] == deadline


@pytest.mark.parametrize('history', [
    [{'storage': 'ambient', 'temperature_c': 25}],
    [{'storage': 'frozen', 'temperature_c': -18}],
    [{'storage': 'refrigerated', 'temperature_c': None}],
    [{'storage': 'refrigerated', 'temperature_c': 8}],
])
def test_interrupted_or_unknown_history_does_not_restart_clock(history, published_rules):
    rule, deadline, reasons = storage_rules.estimate_deadline(payload(storage_history=history))
    assert rule is None and deadline is None and reasons


@pytest.mark.parametrize('storage,temp,rid', [
    ('frozen', -18, 'cooked-frozen'), ('frozen', -30, 'cooked-frozen'),
    ('ambient', 20, 'fresh-produce-ambient-short'),
])
def test_quality_or_handling_reference_is_never_expiry_even_published(storage, temp, rid, published_rules):
    body = payload(storage=storage, temperature_c=temp)
    reference, reasons = lookup(body, rid=rid, published=True, estimates=False)
    assert reference and not reasons and reference['hours'] is None
    rule, deadline, reasons = storage_rules.estimate_deadline(body, rid)
    assert rule is None and deadline is None
    assert any('不提供到期依据' in reason for reason in reasons)
    advice = food_advice.assess(body)
    assert advice['action'] == 'unknown' and advice['date'] is None


def test_missing_soup_temperature_is_missing_info_not_a_guessed_value():
    advice = food_advice.assess(payload(temperature_c=None))
    assert advice['reason'] == 'missing_info' and 'temperature_c' in advice['missing']
    assert advice['date'] is None


def test_passed_estimate_is_retained_as_passed_not_new_duration(published_rules):
    body = payload(started_at=(core.now() - timedelta(days=5)).isoformat())
    advice = food_advice.assess(body)
    assert advice['action'] == 'unknown' and advice['reason'] == 'date_passed'
    assert datetime.fromisoformat(advice['date']) < core.now()


def test_old_or_reference_basis_is_not_effective_date():
    old = payload(storage='frozen', temperature_c=-18, deadline_source='rule',
                  rule_id='cooked-frozen', rule_version='storage-rules-draft-2026-10-24',
                  expires_at=(core.now() + timedelta(days=80)).isoformat())
    before = deepcopy(old)
    result = foods.progress(old)
    assert result['valid_deadline_at'] is None
    assert result['deadline_basis']['type'] == 'none'
    assert result['assessment_status'] == 'needs_review'
    assert result['needs_review'] is True and foods.status(old) == 'needs_review'
    assert old == before  # read-only; no migration or business data rewrite


def test_lookup_preview_and_formal_reevaluation_have_identical_origin(signed, published_rules):
    body = payload()
    preview = signed.post('/api/v1/foods/assess', json=body).json()['suggestion']
    item = signed.post('/api/v1/foods', json=body).json()
    response = signed.post(f"/api/v1/foods/{item['id']}/reevaluate", json={'evidence': {'deadline_source': 'rule'}})
    assert response.status_code == 200
    formal = response.json()
    assert preview['date'] == formal['valid_deadline_at']
    assert preview['basis']['date_origin'] == formal['deadline_basis']['date_origin'] == 'preparation_finished_at'
    assert formal['date_type'] == 'prepared' and formal['elapsed_time_label'] == '制作后经过'


@pytest.mark.parametrize('date_type', ['purchase', 'production', 'opened'])
def test_formal_rejects_wrong_date_meaning_and_commits_pending(signed, published_rules, date_type):
    body = payload(date_type=date_type, deadline_source='user',
                   expires_at=(core.now() + timedelta(days=1)).isoformat(),
                   remind_at=(core.now() + timedelta(hours=1)).isoformat())
    item = signed.post('/api/v1/foods', json=body).json()
    response = signed.post(f"/api/v1/foods/{item['id']}/reevaluate", json={'evidence': {'deadline_source': 'rule'}})
    assert response.status_code == 409 and response.json()['error']['code'] == 'NO_APPLICABLE_RULE'
    assert '日期含义' in response.json()['error']['message']
    formal = signed.get('/api/v1/foods').json()['items'][0]
    assert formal['valid_deadline_at'] is None and formal['display_status'] == 'needs_review'
    assert all(job['status'] == 'cancelled' for job in signed.get('/api/v1/notifications/tasks').json())


def test_freezing_clears_estimate_and_does_not_create_ninety_day_date(signed, published_rules):
    body = payload()
    item = signed.post('/api/v1/foods', json=body).json()
    ruled = signed.post(f"/api/v1/foods/{item['id']}/reevaluate", json={'evidence': {'deadline_source': 'rule',
        'remind_at': (core.now() + timedelta(hours=1)).isoformat()}}).json()
    update = {key: ruled[key] for key in ('name', 'food_class', 'condition', 'storage', 'temperature_c', 'date_type', 'started_at')}
    update.update(storage='frozen', temperature_c=-18)
    frozen = signed.put(f"/api/v1/foods/{item['id']}", json=update).json()
    assert frozen['valid_deadline_at'] is None and frozen['deadline_basis']['type'] == 'none'
    assert frozen['started_at'] == ruled['started_at']
    assert frozen['display_status'] == 'needs_review'
    assert all(job['status'] == 'cancelled' for job in signed.get('/api/v1/notifications/tasks').json())


@pytest.mark.parametrize('note,temp,expected', [
    ('未开封冷藏4℃', 4, True), ('未开封冷藏4℃', 6, False), ('未开封冷藏4℃', 3, False),
    ('不高于4℃冷藏', 3, True), ('不高于4℃冷藏', 6, False),
    ('≤4℃冷藏', 4, True), ('4℃以下冷藏', 2, True),
    ('refrigerate at or below 4°C', 4, True), ('refrigerate at or below 4°C', 5, False),
    ('refrigerate under 4°C', 3, True), ('refrigerate under 4°C', 4, False),
    ('冷藏低于4℃', 4, False), ('冷藏低于4℃', 3, True),
    ('不低于4℃', 3, False), ('不低于4℃', 5, True),
    ('≥4℃', 4, True), ('4℃以上', 5, True),
    ('at or above 4°C', 4, True), ('above 4°C', 4, False),
    ('0—4℃冷藏', 3, True), ('0℃—4℃冷藏', 3, True),
    ('0℃—4℃冷藏', 6, False), ('-20℃至-18℃冷冻', -19, True),
    ('≤-18℃冷冻', -20, True), ('≤-18℃冷冻', -16, False),
    ('4℃左右冷藏', 4, False), ('约4℃冷藏', 4, False),
    ('0—4℃左右冷藏', 3, False), ('≥4℃以下', 4, False),
    ('4℃或8℃冷藏', 4, False), ('40°F冷藏', 4, False),
    ('4℃冷藏', None, False), ('4℃冷藏', float('nan'), False),
])
def test_package_temperature_uses_explicit_bound_without_invented_tolerance(note, temp, expected):
    assert food_advice._temperature_matches(note, {'temperature_c': temp}) is expected


@pytest.mark.parametrize('note,storage,expected', [
    ('未开封冷藏', 'refrigerated', True), ('未开封冷藏', 'frozen', False),
    ('常温或冷藏', 'refrigerated', False), ('未开封常温、开封后冷藏', 'ambient', False),
    ('keep refrigerated', 'ambient', False), ('keep refrigerated', 'refrigerated', True),
    ('keep frozen', 'refrigerated', False), ('keep frozen', 'frozen', True),
    ('do not freeze', 'frozen', False), ('切勿冷冻', 'frozen', False),
])
def test_package_multiple_storage_branches_are_not_silently_selected(note, storage, expected):
    assert food_advice._storage_matches(note, {'storage': storage}) is expected


def test_package_six_degrees_does_not_pass_four_degree_label(signed):
    body = {'name': '豆奶', 'food_class': 'packaged', 'opened': False,
            'storage': 'refrigerated', 'temperature_c': 6,
            'date_type': 'production', 'started_at': (core.now() - timedelta(days=1)).isoformat(),
            'expires_at': (core.now() + timedelta(days=2)).isoformat(),
            'package_note': '未开封4℃冷藏', 'deadline_source': 'package'}
    preview = signed.post('/api/v1/foods/assess', json=body).json()['suggestion']
    assert preview['action'] == 'unknown' and preview['basis']['type'] == 'none'
    item = signed.post('/api/v1/foods', json=body).json()
    assert item['valid_deadline_at'] is None and item['display_status'] == 'needs_review'
    response = signed.post(f"/api/v1/foods/{item['id']}/reevaluate", json={'evidence': {
        'deadline_source': 'package', 'package_note': body['package_note'], 'expires_at': body['expires_at']}})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'PACKAGE_NOT_APPLICABLE'
    assert signed.get('/api/v1/foods').json()['items'][0]['valid_deadline_at'] is None
