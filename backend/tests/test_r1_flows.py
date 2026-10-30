"""R1 四模块对话首轮（B01–B04）验收。

覆盖每条路径的成功、未配置、失败、刷新恢复、重复提交与越权；
全部使用隔离测试库与模拟供应商响应，无真实外部调用。
"""
import io
import struct
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app import core, knowledge, providers, worker
from app.core import now


def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def png_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (40, 40), 'white').save(buf, format='PNG')
    return buf.getvalue()


def wav_bytes(seconds=1.0, rate=16000):
    frames = int(rate * seconds)
    data = b'\x00\x00' * frames
    return (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', len(data)) + data)


def chat(signed):
    return signed.post('/api/v1/chat/sessions', json={}).json()['id']


def say(signed, sid, content, attachments=None):
    body = {'content': content}
    if attachments is not None:
        body['attachments'] = attachments
    return signed.post(f'/api/v1/chat/sessions/{sid}/messages', json=body)


def turn_of(signed, sid, turn_id):
    return signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()


def scripted(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(it))


def ingredient_photo_job(signed, monkeypatch, save_original=False, completed=True):
    """走真实链路：上传 →（替身）提取 → worker.process_once → finish。

    只替换供应商提取函数，图片校验、规则判定、历史写入、清理与任务收敛都真实执行。
    """
    from app.schemas import Extracted
    if completed:
        monkeypatch.setattr(providers, 'extract_image',
                            lambda *a: Extracted(ingredients=['水', '大豆', '洋葱'],
                                                 complete=True,
                                                 label_warnings=['可能含有乳制品']))
    files = {'file': ('label.png', png_bytes(), 'image/png')}
    r = signed.post('/api/v1/ingredients/photo', files=files,
                    data={'save_original': 'true' if save_original else 'false'})
    assert r.status_code == 202, r.text
    job_id = r.json()['job_id']
    if completed:
        worker.process_once()
        assert signed.get(f'/api/v1/jobs/{job_id}').json()['status'] == 'succeeded'
    return job_id, r.json().get('asset_id')


# ================= B01 对话内上传配料表 =================
def test_b01_attachment_produces_card_with_photo_info(signed, monkeypatch):
    enable(monkeypatch)
    job_id, asset_id = ingredient_photo_job(signed, monkeypatch, save_original=True)
    scripted(monkeypatch, {'text': '含洋葱，纯净素不符合。', 'needs_input': False,
                           'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '这包能吃吗', [{'kind': 'ingredient_photo', 'job_id': job_id}]).json()
    worker.process_once()
    data = turn_of(signed, sid, turn['turn_id'])
    assert data['status'] == 'succeeded'
    card = [c for c in data['reply']['cards'] if c['type'] == 'ingredient_result'][0]
    assert card['payload']['result']['results']['pure']['status'] == 'not_suitable'
    assert card['payload']['result']['pungent'] == 'detected'
    # 前端需要的原图信息
    assert card['payload']['photo']['job_id'] == job_id
    assert card['payload']['photo']['saved_original'] is True
    assert card['payload']['photo']['asset_id'] == asset_id
    assert card['payload']['photo']['viewable'] is True
    assert card['payload']['photo']['expires_at']


def test_b01_unsaved_original_reports_no_asset(signed, monkeypatch):
    enable(monkeypatch)
    job_id, _ = ingredient_photo_job(signed, monkeypatch, save_original=False)
    scripted(monkeypatch, {'text': '结果如下。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '看看', [{'kind': 'ingredient_photo', 'job_id': job_id}]).json()
    worker.process_once()
    card = [c for c in turn_of(signed, sid, turn['turn_id'])['reply']['cards']
            if c['type'] == 'ingredient_result'][0]
    assert card['payload']['photo']['saved_original'] is False
    assert card['payload']['photo']['asset_id'] is None


def test_b01_pending_job_requeues_without_fake_result(signed, monkeypatch):
    enable(monkeypatch)
    job_id, _ = ingredient_photo_job(signed, monkeypatch, completed=False)
    sid = chat(signed)
    turn = say(signed, sid, '看这张', [{'kind': 'ingredient_photo', 'job_id': job_id}]).json()
    worker.process_once()
    # 识别未完成：turn 回到排队，不出卡片、不冒充成功
    data = turn_of(signed, sid, turn['turn_id'])
    assert data['status'] == 'queued'
    assert not (data.get('reply') or {}).get('cards')


def test_b01_failed_job_fails_turn_instead_of_success(signed, monkeypatch):
    enable(monkeypatch)
    # 供应商提取失败 → worker 走真实失败路径
    monkeypatch.setattr(providers, 'extract_image',
                        lambda *a: (_ for _ in ()).throw(core.AppError('UPSTREAM_FAILED', '模拟失败')))
    job_id, _ = ingredient_photo_job(signed, monkeypatch, completed=False)
    files = None
    # 上面只创建了任务；这里真正跑一次 worker 让它失败
    worker.process_once()
    assert signed.get(f'/api/v1/jobs/{job_id}').json()['status'] == 'failed'
    sid = chat(signed)
    turn = say(signed, sid, '看这张', [{'kind': 'ingredient_photo', 'job_id': job_id}]).json()
    worker.process_once()
    data = turn_of(signed, sid, turn['turn_id'])
    assert data['status'] == 'failed'
    assert data['error']['code'] == 'PHOTO_FAILED'
    assert '重拍' in data['error']['message']


def test_b01_reusing_same_job_does_not_duplicate_history(signed, monkeypatch):
    enable(monkeypatch)
    job_id, _ = ingredient_photo_job(signed, monkeypatch)
    scripted(monkeypatch, {'text': '一。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    say(signed, sid, '第一次', [{'kind': 'ingredient_photo', 'job_id': job_id}])
    worker.process_once()
    with core.DB() as db:
        before = len(db.scalars(select(core.Record).where(core.Record.kind == 'ingredient')).all())
    scripted(monkeypatch, {'text': '二。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    say(signed, sid, '第二次同一张', [{'kind': 'ingredient_photo', 'job_id': job_id}])
    worker.process_once()
    with core.DB() as db:
        after = len(db.scalars(select(core.Record).where(core.Record.kind == 'ingredient')).all())
    assert before == after  # 复用任务不重复识别、不重复计费、不重复写历史


def test_b01_attachment_ownership_enforced(signed, monkeypatch):
    enable(monkeypatch)
    job_id, _ = ingredient_photo_job(signed, monkeypatch)
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    sid = chat(signed)
    r = say(signed, sid, '抢别人的图', [{'kind': 'ingredient_photo', 'job_id': job_id}])
    assert r.status_code == 404
    assert r.json()['error']['code'] == 'NOT_FOUND'


def test_b01_unconfigured_photo_endpoint_is_explicit(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', False)
    r = signed.post('/api/v1/ingredients/photo',
                    files={'file': ('label.png', png_bytes(), 'image/png')})
    assert r.status_code == 503
    assert r.json()['error']['code'] == 'NOT_CONFIGURED'


# ================= B02 对话内找餐厅 =================
def test_b02_restaurant_card_has_map_ready_fields(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    captured = {}

    def fake_request_json(method, url, **kwargs):
        captured['params'] = kwargs.get('params')
        return {'status': '1', 'pois': [
            {'id': 'B1', 'name': '素心素食餐厅', 'address': '静安某路1号',
             'location': '121.4523,31.2286', 'distance': '850',
             'typecode': '050120', 'type': '餐饮服务;中餐厅;中式素菜馆',
             'biz_ext': {'rating': '4.5', 'cost': '68', 'open_time': '10:00-21:00'}},
            {'id': 'B2', 'name': '无坐标素食餐厅', 'address': '某路2号', 'location': [],
             'typecode': '050120', 'type': '餐饮服务;中餐厅;中式素菜馆', 'biz_ext': {}},
        ]}

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'call_r', 'name': 'search_restaurants',
                                           'arguments': {'location': '121.45,31.23'}}]},
             {'text': '找到 2 家。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '我的本次位置是 121.45,31.23（GCJ-02）。我在静安，找附近素食').json()
    worker.process_once()
    data = turn_of(signed, sid, turn['turn_id'])
    card = [c for c in data['reply']['cards'] if c['type'] == 'restaurant_list'][0]
    payload = card['payload']
    # 地图需要的同批数据与坐标口径
    assert payload['query']['mode'] == 'around'
    assert payload['query']['coordinate_system'] == 'GCJ-02'
    assert payload['items'][0]['location'] == '121.4523,31.2286'
    assert payload['items'][0]['distance_m'] == 850
    assert payload['items'][0]['business_hours'] == '10:00-21:00'
    # 缺坐标不伪造点位
    assert payload['items'][1]['location'] is None
    assert payload['items'][1]['distance_m'] is None
    assert captured['params']['location'] == '121.45,31.23'


def test_b02_area_query_has_no_distance(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(providers, 'request_json',
                        lambda *a, **k: {'status': '1', 'pois': [
                            {'id': 'B1', 'name': '素食店', 'location': '121.4,31.2',
                             'typecode': '050120', 'type': '餐饮服务;中餐厅;中式素菜馆',
                             'biz_ext': {}}]})
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                           'arguments': {'city': '上海'}}]},
             {'text': '结果如下。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '上海有什么素食').json()
    worker.process_once()
    card = [c for c in turn_of(signed, sid, turn['turn_id'])['reply']['cards']
            if c['type'] == 'restaurant_list'][0]
    assert card['payload']['query']['mode'] == 'text'
    assert card['payload']['items'][0]['distance_m'] is None  # 区域查询不冒充“附近”


def test_b02_unconfigured_returns_tool_error(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(core.settings, 'amap_api_key', '')
    calls = []
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                           'arguments': {'city': '上海'}}]},
             {'text': '地图服务未配置。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: (calls.append(h), next(iter([])) if False else None)[1] or _next(calls, monkeypatch, h))
    sid = chat(signed)
    turn = say(signed, sid, '找餐厅').json()
    worker.process_once()
    data = turn_of(signed, sid, turn['turn_id'])
    assert data['status'] == 'succeeded'
    assert not [c for c in (data['reply'] or {}).get('cards', []) if c['type'] == 'restaurant_list']


def _next(calls, monkeypatch, history):
    """辅助：第二个请求返回最终文本（避免在测试里写复杂生成器）。"""
    return {'text': '未配置时说明原因。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}


def test_b02_map_config_states_are_distinct(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_js_key', '')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', '')
    monkeypatch.setattr(core.settings, 'amap_service_host', '')
    data = signed.get('/api/v1/map/config').json()
    assert data['enabled'] is False and data['js_ready'] is False and data['verified'] is False
    monkeypatch.setattr(core.settings, 'amap_js_key', 'k')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 's')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy')
    monkeypatch.setattr(core.settings, 'amap_js_verified', False)
    ready = signed.get('/api/v1/map/config').json()
    assert ready['enabled'] is True and ready['js_ready'] is True
    assert ready['verified'] is False  # 配置不等于验证通过
    assert ready['service_host'].endswith('/_AMapService')
    assert ready['coordinate_system'] == 'GCJ-02'


# ================= B03 对话内找菜谱 / 候选 / 保存 =================
def recipe(title, **kw):
    return {'title': title, 'ingredients': '豆腐、番茄', 'steps': '炒熟。', 'meal': 'main',
            'purpose': 'everyday', 'cuisine': 'chinese', 'confirmed': True, **kw}


def test_b03_local_hit_does_not_search_web(signed, monkeypatch):
    enable(monkeypatch)
    signed.post('/api/v1/recipes', json=recipe('番茄豆腐'))
    called = []
    monkeypatch.setattr(providers, 'search_web', lambda q: called.append(q) or [])
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'search_recipes',
                                           'arguments': {'query': '番茄'}}]},
             {'text': '找到一道。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '找番茄菜').json()
    worker.process_once()
    card = [c for c in turn_of(signed, sid, turn['turn_id'])['reply']['cards']
            if c['type'] == 'recipe_list'][0]
    assert [r['title'] for r in card['payload']['items']] == ['番茄豆腐']
    assert called == []


def test_b03_miss_persists_candidates_with_status(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'search_web', lambda q: [
        {'title': '候选甲', 'source_url': 'https://example.org/a', 'summary': '摘要甲'}])
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'search_web_recipes',
                                           'arguments': {'query': '鹰嘴豆早餐'}}]},
             {'text': '找到一条候选，需要你核对原文。', 'needs_input': False, 'input_prompt': '',
              'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '想找鹰嘴豆早餐').json()
    worker.process_once()
    data = turn_of(signed, sid, turn['turn_id'])
    card = [c for c in data['reply']['cards'] if c['type'] == 'recipe_candidates'][0]
    assert card['status'] == 'pending_confirmation'
    item = card['payload']['items'][0]
    assert item['status'] == 'pending_review'
    assert item['tutorial_complete'] is False
    assert '不是完整教程' in item['notice']
    assert item['source_url'].startswith('https://')
    # 持久化 + 7 天留存 + 计用量 + 不写正式菜谱
    with core.DB() as db:
        candidates = db.scalars(select(core.Record).where(core.Record.kind == 'candidate')).all()
        assert len(candidates) == 1
        assert (candidates[0].expires - now()).days == 6
        assert db.scalars(select(core.Record).where(core.Record.kind == 'recipe')).all() == []
        usage = [r.payload.get('operation') for r in
                 db.scalars(select(core.Record).where(core.Record.kind == 'usage'))]
        assert 'recipe_search' in usage


def test_b03_unconfigured_web_returns_error_not_fake_candidates(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', False)
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    with pytest.raises(core.AppError) as exc:
        agent._execute_tool('search_web_recipes', {'query': '番茄'}, owner, {})
    assert exc.value.code in ('NOT_CONFIGURED', 'EXTERNAL_DISABLED')
    with core.DB() as db:
        assert db.scalars(select(core.Record).where(core.Record.kind == 'candidate')).all() == []


def test_b03_confirm_saves_candidate_once(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'search_web', lambda q: [
        {'title': '候选甲', 'source_url': 'https://example.org/a', 'summary': '摘要'}])
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'search_web_recipes',
                                           'arguments': {'query': '鹰嘴豆'}}]},
             {'text': '请核对原文后确认保存。', 'needs_input': False, 'input_prompt': '',
              'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '找鹰嘴豆菜').json()
    worker.process_once()
    card = [c for c in turn_of(signed, sid, turn['turn_id'])['reply']['cards']
            if c['type'] == 'recipe_candidates'][0]
    candidate_id = card['payload']['items'][0]['candidate_id']

    body = {'title': '鹰嘴豆早餐', 'ingredients': '鹰嘴豆、橄榄油', 'steps': '煮熟拌匀。',
            'meal': 'breakfast', 'confirmed': True}
    first = signed.post(f'/api/v1/recipes/candidates/{candidate_id}/save', json=body)
    assert first.status_code == 201
    again = signed.post(f'/api/v1/recipes/candidates/{candidate_id}/save', json=body)
    assert again.status_code == 200
    assert again.json()['id'] == first.json()['id']      # 重复确认不产生重复记录
    assert again.json()['replayed'] is True
    rows = signed.get('/api/v1/recipes').json()['items']
    saved = [r for r in rows if r.get('origin') == 'web_confirmed_by_user']
    assert len(saved) == 1
    assert saved[0]['candidate_id'] == candidate_id
    # 候选留下但标记已保存，来源可追溯
    with core.DB() as db:
        cand = db.get(core.Record, candidate_id)
        assert cand.payload['status'] == '已保存到个人菜谱'
        assert cand.payload['saved_recipe_id'] == first.json()['id']


def test_b03_unconfirmed_cannot_save(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'search_web', lambda q: [
        {'title': '候选甲', 'source_url': 'https://example.org/a', 'summary': '摘要'}])
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    payload, card = agent._execute_tool('search_web_recipes', {'query': 'x'}, owner, {})
    candidate_id = card['payload']['items'][0]['candidate_id']
    r = signed.post(f'/api/v1/recipes/candidates/{candidate_id}/save',
                    json={'title': '菜', 'ingredients': '豆腐', 'steps': '炒', 'confirmed': False})
    assert r.status_code == 400
    assert r.json()['error']['code'] == 'CONFIRMATION_REQUIRED'
    assert signed.get('/api/v1/recipes').json()['personal_count'] == 0


def test_b03_candidate_save_is_account_scoped(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'search_web', lambda q: [
        {'title': '候选甲', 'source_url': 'https://example.org/a', 'summary': '摘要'}])
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    _, card = agent._execute_tool('search_web_recipes', {'query': 'x'}, owner, {})
    candidate_id = card['payload']['items'][0]['candidate_id']
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    r = signed.post(f'/api/v1/recipes/candidates/{candidate_id}/save',
                    json={'title': '菜', 'ingredients': '豆腐', 'steps': '炒', 'confirmed': True})
    assert r.status_code == 404


# ================= B04 对话内整批记食材 =================
def draft_items():
    return [{'name': '豆腐', 'storage': 'refrigerated', 'date_type': 'purchase',
             'started_at': (now() - timedelta(hours=2)).isoformat()},
            {'name': '西兰花'}]


def test_b04_full_flow_draft_to_fridge(signed, monkeypatch):
    enable(monkeypatch)
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'draft_foods',
                                           'arguments': {'items': draft_items()}}]},
             {'text': '已整理成草稿，请补充西兰花的存放方式和日期。', 'needs_input': True,
              'input_prompt': '西兰花的存放方式与购买日期', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '今天买了豆腐和西兰花，怎么放').json()
    worker.process_once()
    data = turn_of(signed, sid, turn['turn_id'])
    card = [c for c in data['reply']['cards'] if c['type'] == 'food_draft'][0]
    draft_id = card['payload']['draft_id']
    statuses = {i['name']: i['status'] for i in card['payload']['items']}
    assert statuses == {'豆腐': 'ready', '西兰花': 'ready'}
    assert signed.get('/api/v1/foods').json()['items'] == []   # 未确认不入正式记录

    items = {i['name']: i['item_id'] for i in card['payload']['items']}
    signed.put(f"/api/v1/foods/drafts/{draft_id}/items/{items['西兰花']}",
               json={'storage': 'refrigerated', 'started_at': (now() - timedelta(hours=1)).isoformat()})
    result = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={}).json()
    assert result['status'] == 'confirmed'
    fridge = signed.get('/api/v1/foods').json()['items']
    assert {f['name'] for f in fridge} == {'豆腐', '西兰花'}
    assert all(f['display_status'] in ('stored', 'unknown') for f in fridge)


def test_b04_partial_failure_reports_true_state(signed, monkeypatch):
    enable(monkeypatch)
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'draft_foods',
                                           'arguments': {'items': [draft_items()[0], {'name': '西兰花', 'storage': 'invalid'}]}}]},
             {'text': '草稿已生成。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '记一批').json()
    worker.process_once()
    card = [c for c in turn_of(signed, sid, turn['turn_id'])['reply']['cards']
            if c['type'] == 'food_draft'][0]
    draft_id = card['payload']['draft_id']
    result = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={}).json()
    assert result['status'] == 'partially_saved'          # 不声称整批已保存
    assert [r['status'] for r in result['results']] == ['saved', 'failed']
    assert result['results'][1]['error']['code'] == 'NEEDS_INFO'


def test_b04_same_key_replay_does_not_duplicate(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{
        'name': '豆腐', 'storage': 'refrigerated', 'date_type': 'purchase',
        'started_at': (now() - timedelta(hours=1)).isoformat()}]}).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/confirm"
    first = signed.post(path, json={'idempotency_key': 'k1'}).json()
    again = signed.post(path, json={'idempotency_key': 'k1'}).json()
    assert again == first
    assert len(signed.get('/api/v1/foods').json()['items']) == 1


def test_b04_unevidenced_food_still_registrable(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{
        'name': '不明食材', 'storage': 'ambient', 'date_type': 'purchase',
        'started_at': (now() - timedelta(hours=1)).isoformat()}]}).json()
    signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={})
    food = signed.get('/api/v1/foods').json()['items'][0]
    assert food['assessment_status'] == 'unknown'
    assert food['valid_deadline_at'] is None
    assert food['estimated_remaining_days'] is None   # 不编造保质期


def test_b04_draft_is_account_scoped(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '豆腐'}]}).json()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get(f"/api/v1/foods/drafts/{draft['draft_id']}").status_code == 404
    assert signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).status_code == 404


# ================= 预算与恢复（跨四条共性） =================
def test_r1_tool_budget_applies_to_web_recipe_tool(signed, monkeypatch):
    enable(monkeypatch)
    # 放宽外部调用额度，让本用例只验证工具次数上限（额度本身另有测试）
    monkeypatch.setattr(core.settings, 'daily_external_limit', 100)
    monkeypatch.setattr(core.settings, 'weekly_external_limit', 200)
    monkeypatch.setattr(core.settings, 'global_daily_external_limit', 200)
    monkeypatch.setattr(core.settings, 'global_weekly_external_limit', 400)
    monkeypatch.setattr(providers, 'search_web', lambda q: [])
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': '', 'needs_input': False, 'input_prompt': '',
                                   'tool_calls': [{'id': 'c', 'name': 'search_web_recipes',
                                                   'arguments': {'query': 'x'}}]})
    sid = chat(signed)
    turn = say(signed, sid, '一直找').json()
    worker.process_once()
    data = turn_of(signed, sid, turn['turn_id'])
    assert data['status'] == 'failed'
    assert data['error']['code'] == 'TOOL_BUDGET_EXCEEDED'
    assert len(data['cards']) == 6


def test_r1_turn_recovers_after_refresh(signed, monkeypatch):
    enable(monkeypatch)
    scripted(monkeypatch, {'text': '', 'needs_input': False, 'input_prompt': '',
                           'tool_calls': [{'id': 'c1', 'name': 'draft_foods',
                                           'arguments': {'items': [{'name': '豆腐'}]}}]},
             {'text': '草稿已生成。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat(signed)
    turn = say(signed, sid, '记豆腐').json()
    worker.process_once()
    # 刷新后重新读取（新请求、同一账号）仍能取到卡片与状态
    messages = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assert len(messages) == 2
    assert [c['type'] for c in messages[1]['cards']] == ['food_draft']
    assert turn_of(signed, sid, turn['turn_id'])['status'] == 'succeeded'
