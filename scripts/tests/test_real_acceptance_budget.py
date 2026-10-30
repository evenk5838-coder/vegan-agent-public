"""Pure offline checks for the separately approved real-service test budget."""
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('real_acceptance', Path(__file__).resolve().parents[1] / 'real_acceptance.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def approval(path):
    return {'approved': True, 'plan_sha256': module.PLAN_HASH, 'max_cny': 10,
            'max_calls': 40, 'ledger_path': str(path.resolve()), 'authorization_evidence': 'isolated test only',
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}


def test_approval_bound_to_one_immutable_ledger(tmp_path):
    path = tmp_path / 'ledger.json'
    auth = approval(path)
    module.Budget(path, auth)
    with pytest.raises(module.BudgetStop):
        module.Budget(path, auth)
    with pytest.raises(module.BudgetStop):
        module.Budget(tmp_path / 'another.json', auth)


def test_call_hard_cap_precedes_network(tmp_path):
    path = tmp_path / 'ledger.json'
    budget = module.Budget(path, approval(path))
    for _ in range(40):
        budget.reserve('https://restapi.amap.com/v3/place/text', {})
    with pytest.raises(module.BudgetStop):
        budget.reserve('https://restapi.amap.com/v3/place/text', {})
    assert len(json.loads(path.read_text())['calls']) == 40


def test_currency_limit_and_endpoint_allowlist(tmp_path):
    path = tmp_path / 'ledger.json'
    budget = module.Budget(path, approval(path))
    body = {'model': 'deepseek-flash', 'max_tokens': 16384, 'messages': [{'content': 'x' * 500_000}]}
    for _ in range(8):
        budget.reserve('https://api.deepseek.com/chat/completions', {'json': body})
    with pytest.raises(module.BudgetStop):
        budget.reserve('https://api.deepseek.com/chat/completions', {'json': body})
    with pytest.raises(module.BudgetStop):
        budget.reserve('https://not-approved.example/api', {})
    assert budget.state['charged_or_held_micros'] <= 10_000_000


def test_error_stops_later_calls_and_success_receipt_settles_conservatively(tmp_path):
    path = tmp_path / 'ledger.json'
    budget = module.Budget(path, approval(path))
    call = budget.reserve('https://api.deepseek.com/chat/completions',
                          {'json': {'model': 'deepseek-flash', 'max_tokens': 2000}})
    budget.finish(call, {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
    assert budget.state['charged_or_held_micros'] == 60
    call2 = budget.reserve('https://restapi.amap.com/v3/place/text', {})
    budget.finish(call2, failed=True)
    assert budget.state['charged_or_held_micros'] == 10060
    with pytest.raises(module.BudgetStop):
        budget.reserve('https://restapi.amap.com/v3/place/text', {})


def test_followup_counts_previous_calls_cost_and_rejects_changed_receipts(tmp_path):
    first = tmp_path / 'first.json'
    previous = module.Budget(first, approval(first))
    for _ in range(39):
        previous.reserve('https://restapi.amap.com/v3/place/text', {})
    path = tmp_path / 'followup.json'
    auth = {**approval(path), 'prior_ledger': str(first),
            'prior_ledger_sha256': hashlib.sha256(first.read_bytes()).hexdigest()}
    following = module.Budget(path, auth)
    assert following.state['charged_or_held_micros'] == 390_000
    following.reserve('https://restapi.amap.com/v3/place/text', {})
    assert following.state['charged_or_held_micros'] == 400_000
    with pytest.raises(module.BudgetStop):
        following.reserve('https://restapi.amap.com/v3/place/text', {})
    first.write_text('{}')
    third = tmp_path / 'third.json'
    with pytest.raises(module.BudgetStop):
        module.Budget(third, {**auth, 'ledger_path': str(third.resolve())})
