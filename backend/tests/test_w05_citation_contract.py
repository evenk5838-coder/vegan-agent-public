"""Offline replay of the actual W05 citation omission, plus worker persistence.

No paid calls; the receipt and source content are public and redacted. Published
records exist only in the temporary fixture and do not grant source approval.
"""
import copy
import json
from pathlib import Path

import pytest

from app import agent, core, knowledge, providers, worker

EVIDENCE = Path(__file__).resolve().parents[2] / 'docs/evidence/全量收口-2026-10-27'


def receipt():
    return json.loads((EVIDENCE / 'W05真实模型引用-脱敏响应.json').read_text())


def card():
    r = receipt()
    item = next(iter(r['recalled'].values()))
    return {'id': 'offline-card', 'type': 'knowledge_result', 'status': 'info',
            'payload': {'items': [item], 'count': 1}}


def final(**changes):
    parsed = providers._parse_final_content(json.dumps(receipt()['upstream_final_json'], ensure_ascii=False))
    return {**parsed, **changes}


def test_actual_receipt_has_no_structured_citation_to_drop_or_promote():
    r = receipt()
    assert 'citations' not in r['upstream_final_json']
    parsed = final()
    assert parsed['citations'] == []
    assert agent._verify_citations(parsed['citations'], r['recalled']) == []
    # The valid ID in prose is evidence of the omission, not a citation array.
    assert next(iter(r['recalled'])) in parsed['text']


@pytest.mark.parametrize('citations', [None, [], [{'entry_id': 'outside-recall'}]])
def test_substantive_knowledge_answer_requires_whitelist_valid_structured_citation(citations):
    completed = card()
    reply = agent._finalize_reply(final(citations=citations), [completed],
                                  user_text='糖渍橙皮怎么做', recalled=receipt()['recalled'])
    assert reply['error']['code'] == 'KNOWLEDGE_CITATION_REQUIRED'
    assert reply['text'] == ''
    assert reply['citations'] == []
    assert reply['cards'] == [completed]
    assert '上限' not in reply['error']['message']
    assert providers.retryable(reply['error']['code']) is False


def test_valid_citation_retained_and_only_exact_recalled_ids_replaced_in_text():
    r = receipt()
    target = next(iter(r['recalled']))
    unrelated = '00ff00ff00ff00ff00ff00ff00ff00ff'
    reply = agent._finalize_reply(final(text=f'按来源制作。引用条目：{target}。用户提供的编号{unrelated}。',
                                       citations=[{'entry_id': target}, {'entry_id': 'invented'}]),
                                  [card()], user_text='请介绍做法', recalled=r['recalled'])
    assert 'error' not in reply
    assert target not in reply['text']
    assert '《糖渍橙皮》' in reply['text'] and unrelated in reply['text']
    assert reply['citations'] == [r['recalled'][target]]


def test_empty_recall_does_not_require_or_invent_citations():
    reply = agent._finalize_reply(final(text='没有找到对应来源。'), [], user_text='查做法', recalled={})
    assert 'error' not in reply and reply['citations'] == []
    assert reply['text'] == '没有找到对应来源。'


def test_genuine_clarifying_question_with_recall_is_not_failed_as_an_answer():
    reply = agent._finalize_reply(final(text='你想做几份？', needs_input=True, input_prompt='请补充份数。'),
                                  [card()], user_text='先帮我看一下', recalled=receipt()['recalled'])
    assert 'error' not in reply and reply['citations'] == []
    assert reply['needs_input'] is True and reply['text'] == '你想做几份？'


def test_mixed_restaurant_text_suppression_is_not_failed_for_missing_knowledge_citation():
    restaurant = {'type': 'restaurant_list', 'payload': {'items': [{'id': 'public-poi', 'name': '素食餐厅'}]}}
    reply = agent._finalize_reply(final(), [card(), restaurant],
                                  user_text='查北京素食餐厅，也看看菜谱。', restaurant_attempted=True,
                                  recalled=receipt()['recalled'], restaurant_outcome={'status': 'ok'})
    assert 'error' not in reply
    assert reply['restaurant_guard']['model_text_suppressed'] is True
    assert next(iter(receipt()['recalled'])) not in reply['text']


def prepare_replay(signed, monkeypatch, *, citations=None, needs_input=False):
    r = receipt()
    target = next(iter(r['recalled']))
    samples = json.loads((Path(__file__).resolve().parent / 'fixtures/recall-synthetic.json').read_text())['samples']
    source = next(s['entry'] for s in samples if s['pin']['source_id'] == r['source_id'])
    with core.DB() as db:
        payload = {**knowledge.validate_entry(copy.deepcopy(source)), 'entry_id': target,
                   'status': 'published', 'evaluation_fixture_only': True}
        db.add(core.Record(owner='system', kind='knowledge', payload=payload))
        db.commit()
    calls = []
    responses = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'offline-native-tool', 'name': 'search_knowledge',
                         'arguments': {'query': '糖渍橙皮', 'type': 'recipe'},
                         'raw_arguments': '{"query":"糖渍橙皮","type":"recipe"}'}]},
        final(citations=citations, needs_input=needs_input,
              **({'text': '你想做几份？', 'input_prompt': '请补充份数。'} if needs_input else {})),
    ])
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'isolated-citation-test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'reserve_call', lambda *args: None)

    def generate(history):
        calls.append(copy.deepcopy(history))
        return next(responses)

    monkeypatch.setattr(providers, 'generate_chat_reply', generate)
    session_id = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    sent = signed.post(f'/api/v1/chat/sessions/{session_id}/messages',
                       json={'content': '请从知识库查糖渍橙皮，给出做法和来源。'})
    assert sent.status_code == 201
    turn_id = sent.json()['turn_id']
    worker.process_once()
    return session_id, turn_id, calls


def test_real_receipt_replay_persists_failed_state_source_cards_and_no_auto_retry(signed, monkeypatch):
    session_id, turn_id, calls = prepare_replay(signed, monkeypatch)
    turn = signed.get(f'/api/v1/chat/sessions/{session_id}/turns/{turn_id}').json()
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'KNOWLEDGE_CITATION_REQUIRED'
    assert turn['reply'] is None
    assert [c['type'] for c in turn['cards']] == ['knowledge_result']
    assert turn['cards'][0]['payload']['items'][0]['title'] == '糖渍橙皮'
    assert len(calls) == 2
    assert worker.process_once() is False and len(calls) == 2
    with core.DB() as db:
        job = db.get(core.Job, turn_id)
        assert job.status == 'failed' and job.attempts == 1
    messages = signed.get(f'/api/v1/chat/sessions/{session_id}/messages').json()['items']
    assert not any(message['role'] == 'assistant' for message in messages)


def test_valid_counterfactual_structured_citation_survives_worker_persistence(signed, monkeypatch):
    r = receipt()
    target = next(iter(r['recalled']))
    session_id, turn_id, calls = prepare_replay(signed, monkeypatch, citations=[{'entry_id': target}])
    turn = signed.get(f'/api/v1/chat/sessions/{session_id}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded'
    assert turn['reply']['citations'] == [r['recalled'][target]]
    assert target not in turn['reply']['text'] and '《糖渍橙皮》' in turn['reply']['text']
    messages = signed.get(f'/api/v1/chat/sessions/{session_id}/messages').json()['items']
    assistant = next(message for message in messages if message['role'] == 'assistant')
    assert assistant['citations'] == turn['reply']['citations']
    assert target not in assistant['text']


def test_clarifying_question_stays_successful_without_forcing_a_citation(signed, monkeypatch):
    session_id, turn_id, _ = prepare_replay(signed, monkeypatch, needs_input=True)
    turn = signed.get(f'/api/v1/chat/sessions/{session_id}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded' and turn['reply']['needs_input'] is True
    assert turn['reply']['citations'] == []


def test_final_prompt_has_complete_citation_contract_and_ordinary_language_boundary():
    prompt = providers.CHAT_SYSTEM_PROMPT
    assert '"citations":[{"entry_id":"本轮search_knowledge实际返回的entry_id"}]' in prompt
    assert '正文text中不得输出entry_id' in prompt
    assert 'citations必须是空数组[]' in prompt
    assert '不孤立罗列英文解析词' in prompt and '不把未知改成适配' in prompt
