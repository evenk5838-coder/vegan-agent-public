"""Manual, extracted-photo and Agent assessments share real rules/history.

Only image extraction is replaced; use the isolated PostgreSQL test runner.
"""
import io
from datetime import timedelta

from PIL import Image
import pytest
from sqlalchemy import select

from app import agent, core, providers, rules, worker
from app.schemas import Extracted
from app.services import ingredients as ingredient_service


def image_bytes():
    content = io.BytesIO()
    Image.new('RGB', (24, 24), 'white').save(content, format='PNG')
    return content.getvalue()


@pytest.mark.parametrize('complete,avoid_coriander,save_original', [
    (True, False, False), (False, False, True), (True, True, True),
])
def test_three_inputs_share_assessment_history_and_preserve_photo_lifecycle(
        signed, monkeypatch, complete, avoid_coriander, save_original):
    items = ['大豆', '植物鸡（牛奶、鸡蛋）', '洋葱', '香菜', '不明复合调料']
    text = '、'.join(items)
    warnings = ['可能含有乳制品']
    expected = rules.evaluate(text, complete, warnings, avoid_coriander)
    signed.put('/api/v1/preferences', json={'diet': 'vegan', 'avoid_coriander': avoid_coriander})
    real_service = ingredient_service.evaluate_and_record
    sources = []
    def capture(*args, **kwargs):
        sources.append(kwargs['source'])
        return real_service(*args, **kwargs)
    monkeypatch.setattr(ingredient_service, 'evaluate_and_record', capture)

    manual = signed.post('/api/v1/ingredients/evaluate', json={
        'text': text, 'complete': complete, 'label_warnings': warnings})
    assert manual.status_code == 200
    manual = manual.json()
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    tool_result, card = agent._execute_tool('evaluate_ingredients', {
        'text': text, 'complete': complete, 'label_warnings': warnings}, owner,
        {'avoid_coriander': avoid_coriander})

    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'synthetic-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'extract_image', lambda *_args: Extracted(
        ingredients=items, complete=complete, label_warnings=warnings))
    uploaded = signed.post('/api/v1/ingredients/photo',
        files={'file': ('label.png', image_bytes(), 'image/png')},
        data={'save_original': 'true' if save_original else 'false'})
    assert uploaded.status_code == 202
    job_id = uploaded.json()['job_id']
    with core.DB() as db:
        photo_payload = db.get(core.Job, job_id).payload
        asset_id = photo_payload['asset_id']
        filename = photo_payload['filename']
    assert worker.process_once() is True
    job = signed.get(f'/api/v1/jobs/{job_id}').json()
    assert job['status'] == 'succeeded'
    photo_result = job['result']

    assert {key: value for key, value in manual.items() if key != 'id'} == expected
    assert {key: value for key, value in tool_result.items() if key != 'history_id'} == expected
    assert card['payload'] == {'history_id': tool_result['history_id'], 'result': expected}
    assert {key: value for key, value in photo_result.items() if key != 'history_id'} == expected
    assert sources == ['manual', 'agent', 'photo']

    ids = [manual['id'], tool_result['history_id'], photo_result['history_id']]
    with core.DB() as db:
        histories = [db.get(core.Record, identifier) for identifier in ids]
        assert len(set(ids)) == 3
        assert [history.payload['source'] for history in histories] == sources
        for history in histories:
            assert history.owner == owner and history.kind == 'ingredient'
            assert history.payload['text'] == text and history.payload['result'] == expected
            assert timedelta(days=89) < history.expires - core.now() <= timedelta(days=90)
        assert histories[2].payload['photo'] == {'saved_original': save_original, 'asset_id': asset_id}
        assert 'filename' not in histories[2].payload and filename not in str(histories[2].payload)
        assert db.get(core.Job, job_id).payload == {'history_id': ids[2]}
        assert (db.get(core.Record, asset_id) is not None) is save_original
    assert (core.settings.data_dir / 'images' / filename).exists() is save_original
    assert {entry['id'] for entry in signed.get('/api/v1/ingredients/history').json()} == set(ids)


def test_shared_history_participates_in_callers_transaction_without_commit(signed):
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
        _, history = ingredient_service.evaluate_and_record(db, owner, '水、大豆',
            complete=True, label_warnings=[], source='manual')
        identifier = history.id
        assert identifier
        db.rollback()
    with core.DB() as db:
        assert db.get(core.Record, identifier) is None


def test_photo_metadata_is_detached_without_changing_result_or_evidence(signed):
    photo = {'saved_original': True, 'asset_id': 'synthetic-asset'}
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
        result, history = ingredient_service.evaluate_and_record(db, owner, ' 水 、 大豆 ',
            complete=False, label_warnings=['配料可能不完整'], source='photo', photo=photo)
        photo['asset_id'] = 'changed-after-assessment'
        assert history.payload['photo']['asset_id'] == 'synthetic-asset'
        assert history.payload['text'] == ' 水 、 大豆 '
        assert history.payload['result'] == result
        db.rollback()
