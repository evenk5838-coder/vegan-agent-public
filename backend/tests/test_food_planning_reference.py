"""Source notes are conditional display data, not effective expiry evidence."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app import core, storage_rules
from app.services import food_advice
from app.services.food_planning_reference import LEAF_SOURCE, TOFU_SOURCE, SOUP_SOURCE


def food(**changes):
    return {'name': '菠菜', 'food_class': 'fresh', 'condition': 'raw',
            'storage': 'refrigerated', 'date_type': 'purchase',
            'started_at': (core.now() - timedelta(hours=1)).isoformat(), **changes}


@pytest.mark.parametrize('name', ['菠菜', '生菜', '油麦菜', '小白菜', '上海青', '青菜'])
def test_exact_leaf_names_show_conditioned_original_source_without_expiry(name):
    suggestion = food_advice.assess(food(name=name))
    reference = suggestion['usage']['reference']
    assert reference['source_url'] == LEAF_SOURCE and '2017' in reference['source_title']
    assert reference['kind'] == 'planning_reference'
    assert '3天' in reference['text'] and '未确认' in reference['text']
    assert '不是安全保证' in reference['text'] and '自动期限' in reference['text']
    assert any('软纸' in value and '保鲜袋' in value for value in reference['conditions'])
    assert suggestion['action'] == 'unknown' and suggestion['date'] is None
    assert suggestion['basis']['type'] == 'none' and suggestion['rules_status'] == 'none'


@pytest.mark.parametrize('changes', [
    {'name': '菠菜沙拉'}, {'name': '白菜'}, {'name': '冷藏菠菜'},
    {'food_class': 'unknown'}, {'condition': 'cut'}, {'condition': 'cooked'},
    {'storage': 'ambient'}, {'storage': 'frozen'}, {'storage': 'unknown'},
    {'date_type': 'unknown'}, {'started_at': None},
    {'started_at': (core.now() + timedelta(hours=1)).isoformat()},
    {'status': 'used'}, {'status': 'discarded'}, {'needs_review': True},
    {'deadline_source': 'user', 'expires_at': (core.now() - timedelta(hours=1)).isoformat()},
    {'deadline_source': 'package', 'package_note': '冷冻', 'expires_at': (core.now() + timedelta(days=1)).isoformat()},
])
def test_unknown_inapplicable_ended_or_adverse_records_never_show_reference(changes):
    assert 'reference' not in food_advice.assess(food(**changes))['usage']


def test_exact_tofu_gets_no_numeric_days_or_unopened_date_extension():
    suggestion = food_advice.assess(food(name='豆腐'))
    reference = suggestion['usage']['reference']
    assert reference['source_url'] == TOFU_SOURCE and '2023-12-11' in reference['source_title']
    assert '尽快食用' in reference['text'] and '不能继续沿用未开封保质期' in reference['text']
    assert not any(char.isdigit() for char in reference['text'])
    assert suggestion['action'] == 'unknown' and suggestion['date'] is None
    for name in ['腐竹', '臭豆腐', '鸡蛋豆腐']:
        assert 'reference' not in food_advice.assess(food(name=name))['usage']


def test_cooked_soup_reference_keeps_unconfirmed_handling_separate_from_recorded_facts():
    payload = food(name='蔬菜汤', food_class='cooked', condition='cooked', date_type='prepared')
    suggestion = food_advice.assess(payload)
    reference = suggestion['usage']['reference']
    assert reference['source_url'] == SOUP_SOURCE and '3–4天' in reference['text']
    assert '不高于4℃' in reference['text'] and '未由登记记录完整证明' in reference['text']
    assert suggestion['reason'] == 'missing_info' and 'temperature_c' in suggestion['missing']
    assert suggestion['date'] is None and suggestion['basis']['type'] == 'none'
    for changes in [{'temperature_c': 5}, {'temperature_c': -2}, {'date_type': 'purchase'},
                    {'condition': 'raw'}, {'name': '汤'},
                    {'storage_history': [{'storage': 'ambient'}]}]:
        assert 'reference' not in food_advice.assess({**payload, **changes})['usage']


def test_effective_package_date_wins_and_reference_never_replaces_it():
    expiry = (core.now() + timedelta(days=1)).isoformat()
    payload = food(name='豆腐', food_class='packaged', opened=False,
                   date_type='production', deadline_source='package',
                   package_note='未开封冷藏保存', expires_at=expiry)
    suggestion = food_advice.assess(payload)
    assert suggestion['basis']['type'] == 'package' and suggestion['date'] == expiry
    assert suggestion['action'] == 'date' and 'reference' not in suggestion['usage']


def test_display_reference_survives_confirm_but_never_creates_expiry_reminders_or_publication(signed):
    before = deepcopy(storage_rules.RULES)
    draft = signed.post('/api/v1/foods/drafts', json={'items': [food()]}).json()
    assert draft['items'][0]['suggestion']['usage']['reference']['source_url'] == LEAF_SOURCE
    result = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={
        'advice_digest': draft['advice_digest'], 'idempotency_key': 'conditional-reference'}).json()
    assert result['status'] == 'confirmed'
    saved = signed.get('/api/v1/foods').json()['items'][0]
    assert saved['suggestion']['usage']['reference']['source_url'] == LEAF_SOURCE
    assert saved['expires_at'] is None and saved['valid_deadline_at'] is None
    assert saved['estimated_remaining_days'] is None and saved['deadline_source'] == 'unknown'
    assert signed.get('/api/v1/notifications/tasks').json() == []
    assert storage_rules.RULES == before and all(rule['status'] == 'draft' for rule in before)
