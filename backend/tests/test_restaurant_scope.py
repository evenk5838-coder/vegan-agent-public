"""餐厅推荐内容边界回归：单店类别准入、分页保持、模型旁路封堵。

对应 2026-10-25 验看问题：卡片混入美甲店、牛肉米线、炸串等，
以及把供应商匹配数称作“库内共 N 家”。

全部离线；供应商响应为替身，真实外部调用在 conftest 中已被阻断。
"""
import pytest

from app import core, providers, worker
from app.services import restaurant_scope


def poi(pid, name, typecode=None, type_=None, **kw):
    item = {'id': pid, 'name': name, 'address': '某路1号', 'location': '116.40,39.90',
            'distance': '800', 'biz_ext': {'rating': '4.4', 'cost': '60'}}
    if typecode is not None:
        item['typecode'] = typecode
    if type_ is not None:
        item['type'] = type_
    item.update(kw)
    return item


VEG_CODE = '050120'
VEG_TYPE = '餐饮服务;中餐厅;中式素菜馆'


def fake_amap(monkeypatch, pois, count=None):
    state = {'calls': []}

    def fake(method, url, **kwargs):
        state['calls'].append(kwargs.get('params'))
        payload = {'status': '1', 'pois': pois}
        if count is not None:
            payload['count'] = count
        return payload

    monkeypatch.setattr(providers, 'request_json', fake)
    return state


def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


# ---------------- 准入规则单元 ----------------
@pytest.mark.parametrize('item,ok,reason', [
    # 用户报告的真实混入项
    (poi('1', '荷塘月色·欧尚美甲美睫(天通尾货店)', '060101', '购物服务;美容美发店;美容美发店'), False, 'non_food'),
    (poi('2', '云阿蛮云南生烫牛肉米线(通州领展店)', '050100', '餐饮服务;中餐厅;中餐厅'), False, 'meat'),
    (poi('3', '夸父炸串(垡头店)', '050300', '餐饮服务;快餐厅;快餐厅'), False, 'meat'),
    (poi('4', '某牛肉面馆', '050100', '餐饮服务;中餐厅;牛肉面'), False, 'meat'),
    # 正常素食店
    (poi('5', '随喜赞素食养生餐厅', VEG_CODE, VEG_TYPE), True, 'veg_category'),
    (poi('6', '某素斋', None, '餐饮服务;中餐厅;素菜'), True, 'veg_type'),
    # 多分类仍需店名自身的明确身份；单字素不能补充独立证据
    (poi('7', '素心素食小馆', '050100;050120', '餐饮服务;中餐厅;中餐厅'), True, 'veg_category'),
    # 仿荤词不误杀，但仍需素食身份和餐饮类别双证据
    (poi('8', '素食素鸭王', VEG_CODE, VEG_TYPE), True, 'veg_category'),
    (poi('9', '素斋素鸡坊', None, '餐饮服务;中餐厅;素菜'), True, 'veg_type'),
    (poi('10', '植物料理植物肉餐厅', VEG_CODE, VEG_TYPE), True, 'veg_category'),
    # 类别缺失 → 不作为蔬食推荐
    (poi('11', '无名小店'), False, 'missing_category'),
    (poi('12', '只有类型没码', None, '餐饮服务;中餐厅;中餐厅'), False, 'no_veg_evidence'),
    # 类别冲突（同时有素食与荤食线索）
    (poi('13', '混合店', VEG_CODE, '餐饮服务;中餐厅;素菜;烤肉'), False, 'conflict'),
])
def test_admission_rules(item, ok, reason):
    verdict = restaurant_scope.admit(item)
    assert verdict['ok'] is ok
    assert verdict['reason'] == reason


def test_admission_ignores_shop_name_only():
    """店名带“素”但类别是荤食 → 不因店名放行。"""
    verdict = restaurant_scope.admit(poi('1', '素味烧烤', '050100', '餐饮服务;中餐厅;烧烤'))
    assert verdict['ok'] is False


# ---------------- 接口层：过滤与计数 ----------------
def test_http_filters_conflicting_pois(signed, monkeypatch):
    enable(monkeypatch)
    fake_amap(monkeypatch, [
        poi('A1', '随喜赞素食养生餐厅', VEG_CODE, VEG_TYPE),
        poi('A2', '荷塘月色·欧尚美甲美睫', '060101', '购物服务;美容美发店;美容美发店'),
        poi('A3', '云阿蛮云南生烫牛肉米线', '050100', '餐饮服务;中餐厅;牛肉面'),
        poi('A4', '仙子亭素食餐厅', VEG_CODE, VEG_TYPE),
    ], count='68')
    data = signed.get('/api/v1/restaurants?city=北京').json()
    names = [i['name'] for i in data['items']]
    assert names == ['随喜赞素食养生餐厅', '仙子亭素食餐厅']
    page = data['pagination']
    assert page['returned'] == 2 and page['raw_returned'] == 4
    assert page['filtered_out'] == 2                 # 过滤数单独计，不冒充 deduplicated
    assert page['deduplicated'] == 0
    assert page['reject_reasons'] == {'non_food': 1, 'meat': 1}   # 牛肉面 属明确荤食类别
    # 供应商匹配数不等于本产品店铺总数
    assert data['notice'].count('不等于') >= 1
    assert data['scope']['filtered_out'] == 2
    assert '未逐店核验菜单' in data['scope']['text']


def test_agent_card_uses_same_filtered_result(signed, monkeypatch):
    enable(monkeypatch)
    fake_amap(monkeypatch, [
        poi('A1', '素心素食餐厅', VEG_CODE, VEG_TYPE),
        poi('A2', '牛肉米线店', '050100', '餐饮服务;中餐厅;牛肉面'),
    ], count='2')
    from app import agent
    with core.DB() as db:
        owner = db.scalar(__import__('sqlalchemy').select(core.User)).id
    payload, card = agent._execute_tool('search_restaurants', {'city': '北京'}, owner, {})
    assert [i['name'] for i in payload['items']] == ['素心素食餐厅']
    assert [i['name'] for i in card['payload']['items']] == ['素心素食餐厅']
    assert card['payload']['pagination']['filtered_out'] == 1


def test_filtered_empty_page_still_paginates(signed, monkeypatch):
    """本页全被过滤但上游还有下一页：items 为空，has_more/next_page 保留。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [
        poi('B1', '美甲店', '060101', '购物服务;美容美发店'),
        poi('B2', '牛肉面', '050100', '餐饮服务;中餐厅;牛肉面'),
    ], count='50')
    data = signed.get('/api/v1/restaurants?city=北京&page=2&page_size=20').json()
    assert data['items'] == []
    page = data['pagination']
    assert page['has_more'] is True and page['next_page'] == 3
    assert page['filtered_out'] == 2
    assert '不代表该地区没有素食餐厅' in data['notice']
    # 不自动补页：只请求了一次
    assert len(fake_amap(monkeypatch, [], count='0')['calls']) == 0


def test_no_auto_extra_request_when_page_filtered_out(signed, monkeypatch):
    enable(monkeypatch)
    state = fake_amap(monkeypatch, [poi('C1', '美甲店', '060101', '购物服务;美容美发店')], count='50')
    signed.get('/api/v1/restaurants?city=北京&page=1')
    assert len(state['calls']) == 1


def test_count_not_presented_as_city_total(signed, monkeypatch):
    enable(monkeypatch)
    fake_amap(monkeypatch, [poi('D1', '素心素食餐厅', VEG_CODE, VEG_TYPE)], count='68')
    data = signed.get('/api/v1/restaurants?city=北京').json()
    assert data['pagination']['matched_count'] == 68
    assert '库内' not in data['notice']
    assert data['pagination']['count_source'] == 'provider_reported'


# ---------------- 模型旁路 ----------------
def _turn(signed, monkeypatch, replies, content='北京有什么餐厅，有烤鸭吗'):
    it = iter(replies)
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(it))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': content}).json()['turn_id']
    worker.process_once()
    return signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()


def test_model_cannot_inject_restaurant_card(signed, monkeypatch):
    """模型在回复里自带 cards 时一律丢弃，只保留工具/附件产生的卡片。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [], count='0')     # 服务端补查也为空
    forged = {'text': '推荐金鼎轩。', 'needs_input': False, 'input_prompt': '', 'tool_calls': [],
              'cards': [{'id': 'forged', 'type': 'restaurant_list', 'status': 'info',
                         'payload': {'items': [{'id': 'X1', 'name': '金鼎轩'}], 'pagination': {}}}]}
    data = _turn(signed, monkeypatch,
                 [forged, {'text': '没有查到。', 'needs_input': False,
                           'input_prompt': '', 'tool_calls': []}])
    reply = data['reply']
    assert all(c.get('id') != 'forged' for c in reply['cards'])   # 伪造卡片被丢弃
    assert all(c.get('payload', {}).get('items') != [{'id': 'X1', 'name': '金鼎轩'}]
               for c in reply['cards'])
    assert reply['restaurant_guard']['has_restaurant_result'] is False
    assert reply['places'] == []


def test_area_without_tool_is_routed_deterministically(signed, monkeypatch):
    """有明确地区但模型没调工具：服务端按只读任务补查，不反问、不强加确认。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [], count='0')            # 补查结果为空
    data = _turn(signed, monkeypatch,
                 [{'text': '北京有很多素食餐厅，比如某家。', 'needs_input': False,
                   'input_prompt': '', 'tool_calls': []},
                  {'text': '这次没有查到。', 'needs_input': False,
                   'input_prompt': '', 'tool_calls': []}],
                 content='北京有什么餐厅')
    reply = data['reply']
    assert reply['restaurant_guard']['restaurant_attempted'] is True   # 真的去查了
    assert reply['restaurant_guard']['status'] == 'empty'
    assert '某家' not in reply['text']
    assert reply['places'] == []


def test_tool_failure_does_not_allow_invented_places(signed, monkeypatch):
    enable(monkeypatch)

    def boom(*a, **k):
        raise core.AppError('UPSTREAM_FAILED', '外部服务未正常返回，请稍后重试。', 502)

    monkeypatch.setattr(providers, 'request_json', boom)
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '查到几家不错的素食餐厅。', 'needs_input': False, 'input_prompt': '',
                'tool_calls': []}]
    reply = _turn(signed, monkeypatch, replies)['reply']
    assert reply['places'] == []
    assert reply['restaurant_guard']['clarification_added'] is True


def test_places_only_contain_admitted_items(signed, monkeypatch):
    enable(monkeypatch)
    fake_amap(monkeypatch, [
        poi('E1', '素心素食餐厅', VEG_CODE, VEG_TYPE),
        poi('E2', '美甲店', '060101', '购物服务;美容美发店'),
    ], count='2')
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '这家不错。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}]
    data = _turn(signed, monkeypatch, replies)
    reply = data['reply']
    assert [p['name'] for p in reply['places']] == ['素心素食餐厅']
    assert reply['restaurant_guard']['allowed_place_ids'] == ['E1']
    card = [c for c in reply['cards'] if c['type'] == 'restaurant_list'][0]
    assert len(card['payload']['items']) == len(reply['places'])   # 正文/卡片/受控引用同源


def test_card_and_text_counts_consistent(signed, monkeypatch):
    enable(monkeypatch)
    fake_amap(monkeypatch, [poi(f'F{i}', f'素食店{i}', VEG_CODE, VEG_TYPE) for i in range(3)],
              count='30')
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '共找到 3 家。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}]
    data = _turn(signed, monkeypatch, replies)
    reply = data['reply']
    card = [c for c in reply['cards'] if c['type'] == 'restaurant_list'][0]
    assert card['payload']['pagination']['returned'] == 3
    assert len(reply['places']) == 3


# ================= 复审收尾（R1/R2） =================
@pytest.mark.parametrize('item,reason', [
    ({'typecode': '060101', 'type': '素食'}, 'conflict'),                 # 非餐饮码 + 素食字样
    ({'typecode': '050120', 'type': '生活服务;美甲美睫'}, 'conflict'),      # 素食码 + 非餐饮类目
    ({'typecode': '0501209', 'type': ''}, 'invalid_category'),            # 畸形叶子码
    ({'typecode': '050120', 'type': '餐饮服务;中餐厅;素菜;烤肉'}, 'conflict'),  # 素+荤
    ({'typecode': '060101', 'type': '购物服务;美容美发店'}, 'non_food'),
])
def test_review_conflict_cases_are_rejected(item, reason):
    verdict = restaurant_scope.admit(item)
    assert verdict['ok'] is False
    assert verdict['reason'] == reason


def test_vegetable_code_must_match_leaf_exactly():
    """叶子码须精确，且单码本身再不能作为完整素食准入证据。"""
    assert restaurant_scope.admit({'typecode': '050120'})['ok'] is False
    assert restaurant_scope.admit({'name': '明确素食馆', 'typecode': '050120'})['ok'] is True
    assert restaurant_scope.admit({'typecode': '0501209'})['ok'] is False
    assert restaurant_scope.admit({'typecode': '0501'})['ok'] is False


@pytest.mark.parametrize('name', [
    # 2026-10-27真实公开查询：这些POI均被上游误标050120/中式素菜馆。
    '粤知一二隆江猪脚饭(望京新世界店)', '陕西手擀面·川湘菜',
    '轻易轻食(暖山生活店)', '护国寺小吃(北京武圣东里店)', '馅老满(东四店)',
    '麦田归来·四季轻食(亦庄店)', '港师傅菠萝包(广安门天虹店)',
    # 无来源核验的名称，不凭品牌记忆或任意“素”字放行。
    '素养·养生汤(昌平店)', '素满香·全民食养自助(北京首店)', '未知小馆',
])
def test_v3_misclassified_real_pois_need_independent_name_identity(name):
    assert restaurant_scope.admit(poi('misclassified', name, VEG_CODE, VEG_TYPE))['ok'] is False


@pytest.mark.parametrize('name', [
    '素食馆·隆江猪脚饭', '蔬食餐厅·牛肉面', '素斋·手撕鸡', '全素厨房·烤鸭',
    '植物牛肉素食馆·猪脚饭', '素鸡素食坊·羊蝎子', 'Vegan Kitchen Beef House',
    '非素食烤鸭店', 'Non-Vegan Restaurant',
])
def test_v3_vegetarian_words_never_hide_other_meat_conflicts(name):
    assert restaurant_scope.admit(poi('conflict', name, VEG_CODE, VEG_TYPE))['ok'] is False


@pytest.mark.parametrize('name', [
    '素食素鸭馆', '植物鸡素食馆', '植物肉蔬食餐厅', '全素植物牛肉汉堡',
    '素食素烤鸭小馆', 'Vegan Chicken Restaurant', 'Vegetarian Fish Kitchen',
    'Plant-based Beef Restaurant', '纯素米线馆', '素食拉面馆',
])
def test_v3_local_plant_food_protection_does_not_ban_generic_foods(name):
    assert restaurant_scope.admit(poi('plant', name, VEG_CODE, VEG_TYPE))['ok'] is True


@pytest.mark.parametrize('item', [
    poi('non-food', '素食美甲店', VEG_CODE, VEG_TYPE),
    poi('non-food-code', '纯素生活馆', '060101', '购物服务'),
    poi('no-category', 'Vegan Restaurant'),
    poi('not-food', 'Vegan Store', None, '素食'),
    poi('false-token', 'Veganism Kitchen', VEG_CODE, VEG_TYPE),
])
def test_v3_identity_cannot_replace_food_category_or_override_nonfood(item):
    assert restaurant_scope.admit(item)['ok'] is False


def test_v3_http_and_agent_share_real_misclassification_filter(signed, monkeypatch):
    enable(monkeypatch)
    fake_amap(monkeypatch, [
        poi('safe', '随喜赞素食养生餐厅', VEG_CODE, VEG_TYPE),
        poi('pork', '粤知一二隆江猪脚饭(望京新世界店)', VEG_CODE, VEG_TYPE),
        poi('unknown', '馅老满(东四店)', VEG_CODE, VEG_TYPE),
        poi('light', '轻易轻食(暖山生活店)', VEG_CODE, VEG_TYPE),
        poi('fake-veg', '素食馆·牛肉面', VEG_CODE, VEG_TYPE),
    ], count='50')
    page = signed.get('/api/v1/restaurants?city=北京').json()
    assert [item['id'] for item in page['items']] == ['safe']
    assert page['pagination']['raw_returned'] == 5
    assert page['pagination']['filtered_out'] == 4
    assert page['pagination']['has_more'] is True
    assert page['scope']['version'] == 'veg-admission-v3'
    assert page['scope']['identity_basis'] == 'explicit_vegetarian_name'
    assert '不能单独作为依据' in page['scope']['text']
    from app import agent
    with core.DB() as db:
        owner = db.scalar(__import__('sqlalchemy').select(core.User)).id
    payload, card = agent._execute_tool('search_restaurants', {'city': '北京'}, owner, {})
    assert [item['id'] for item in payload['items']] == ['safe']
    assert [item['id'] for item in card['payload']['items']] == ['safe']
    assert all(item['scope']['version'] == 'veg-admission-v3' for item in payload['items'])


def test_no_tool_no_place_text_is_replaced_not_appended(signed, monkeypatch):
    """无工具时，餐厅问题整段由服务端生成，模型文字不展示（不靠关键词过滤）。"""
    enable(monkeypatch)
    data = _turn(signed, monkeypatch,
                 [{'text': '全聚德可以试试。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}],
                 content='有素食馆吗？')
    text = data['reply']['text']
    assert '全聚德' not in text
    assert data['reply']['restaurant_guard']['model_text_suppressed'] is True
    # 无地点线索 → 明确追问地区，而不是笼统说“没有结果”
    assert data['reply']['restaurant_guard']['status'] == 'missing_area'
    assert '哪个城市' in text


def test_with_result_text_only_cites_admitted_places(signed, monkeypatch):
    """有合格结果时，模型另荐的店名也必须被移除，只保留服务端生成段。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [poi('G1', '素心素食餐厅', VEG_CODE, VEG_TYPE)], count='1')
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '推荐全聚德烤鸭店。素心素食餐厅也可以。', 'needs_input': False,
                'input_prompt': '', 'tool_calls': []}]
    reply = _turn(signed, monkeypatch, replies)['reply']
    assert '全聚德' not in reply['text']
    assert '素心素食餐厅' in reply['text']          # 服务端用合格结果重写
    assert [p['name'] for p in reply['places']] == ['素心素食餐厅']
    assert reply['restaurant_guard']['recommendation_source'] == 'server'


def test_non_restaurant_question_keeps_model_text(signed, monkeypatch):
    """非餐厅问题：正文原样保留，不插入餐厅段落（原来会误删菜谱名）。"""
    enable(monkeypatch)
    data = _turn(signed, monkeypatch,
                 [{'text': '推荐燕麦豆乳粥。搭配坚果可以增加脂肪来源。',
                   'needs_input': False, 'input_prompt': '', 'tool_calls': []}],
                 content='早餐吃什么？')
    text = data['reply']['text']
    assert '燕麦豆乳粥' in text
    assert '素食餐厅结果' not in text
    assert data['reply']['restaurant_guard']['restaurant_turn'] is False


def test_history_keeps_same_constraint_after_refresh(signed, monkeypatch):
    """持久化消息与读回同样带 places/guard，刷新后不回到未过滤正文。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [poi('H1', '素心素食餐厅', VEG_CODE, VEG_TYPE)], count='1')
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '推荐全聚德。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}]
    it = iter(replies)
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(it))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                json={'content': '北京有什么餐厅'})
    worker.process_once()
    items = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assistant = [m for m in items if m['role'] == 'assistant'][0]
    assert '全聚德' not in assistant['text']
    assert [p['name'] for p in assistant['places']] == ['素心素食餐厅']
    assert assistant['restaurant_guard']['has_restaurant_result'] is True


def test_legacy_message_without_places_reads_back_space(signed):
    """旧消息没有新字段：places 返回 []，guard 为 null，前端可按“旧结果”处理。"""
    with core.DB() as db:
        owner = db.scalar(__import__('sqlalchemy').select(core.User)).id
        session = core.Record(owner=owner, kind='chat_session', payload={'title': 't'})
        db.add(session)
        db.flush()
        db.add(core.Record(owner=owner, kind='chat_message',
                           payload={'session_id': session.id, 'role': 'assistant',
                                    'content_type': 'text', 'text': '旧回复', 'seq': 1}))
        db.commit()
        sid = session.id
    item = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items'][0]
    assert item['places'] == []
    assert item['restaurant_guard'] is None


# ================= A：正文受控 + 规则版本（复审 3 反例） =================
@pytest.mark.parametrize('user_text,model_text', [
    ('有素食馆吗？', '全聚德可以试试。'),                          # 反例①：无卡片
    ('有素食馆吗？', '推荐全聚德。\n\nQuanjude is great.'),        # 换行+英文改写
    ('那家店人均多少？', '全聚德可以试试。'),                          # 追问
])
def test_restaurant_turn_never_shows_model_shop_text(signed, monkeypatch, user_text, model_text):
    enable(monkeypatch)
    data = _turn(signed, monkeypatch,
                 [{'text': model_text, 'needs_input': False, 'input_prompt': '', 'tool_calls': []}],
                 content=user_text)
    reply = data['reply']
    assert reply is not None, data
    assert '全聚德' not in reply['text']
    assert 'Quanjude' not in reply['text']
    assert reply['restaurant_guard']['model_text_suppressed'] is True


def test_restaurant_turn_with_card_also_suppresses_model_text(signed, monkeypatch):
    """反例②：有合格卡时，模型另荐的店同样不出现。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [poi('V1', '素心素食餐厅', VEG_CODE, VEG_TYPE)], count='1')
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '全聚德可以试试。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}]
    reply = _turn(signed, monkeypatch, replies)['reply']
    assert '全聚德' not in reply['text']
    assert '素心素食餐厅' in reply['text']
    assert [p['name'] for p in reply['places']] == ['素心素食餐厅']


def test_recipe_question_not_misdeleted(signed, monkeypatch):
    """反例③：菜谱问题不得被删菜名、也不得插入“没有素食餐厅结果”。"""
    enable(monkeypatch)
    data = _turn(signed, monkeypatch,
                 [{'text': '推荐燕麦豆乳粥。搭配坚果可以增加脂肪来源。',
                   'needs_input': False, 'input_prompt': '', 'tool_calls': []}],
                 content='早餐吃什么？')
    text = data['reply']['text']
    assert '燕麦豆乳粥' in text and '搭配坚果' in text
    assert '素食餐厅结果' not in text


def test_mixed_question_keeps_other_module_cards(signed, monkeypatch):
    """混合问题保留规则结论，不恢复含肉店推荐的模型原文。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [poi('W1', '素心素食餐厅', VEG_CODE, VEG_TYPE)], count='1')
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'evaluate_ingredients',
                                'arguments': {'text': '小麦粉、奶粉', 'complete': True}},
                               {'id': 'c2', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '这包含奶粉；附近推荐全聚德。', 'needs_input': False,
                'input_prompt': '', 'tool_calls': []}]
    reply = _turn(signed, monkeypatch, replies, content='这包能吃吗？北京附近有素食馆吗？')['reply']
    types = [c['type'] for c in reply['cards']]
    assert 'ingredient_result' in types and 'restaurant_list' in types
    assert '全聚德' not in reply['text']
    assert '纯净素：不适合' in reply['text']
    assert 'Vegan：不适合' in reply['text']
    assert '蛋奶素：适合' in reply['text']
    assert '其他结果见下方卡片' not in reply['text']


def test_mixed_ingredient_summary_preserves_unknown_and_never_uses_free_text():
    from app.agent import _ingredient_conclusion
    assert _ingredient_conclusion({'payload': {'result': {'results': {
        'pure': {'status': 'uncertain'}, 'vegan': {'status': 'uncertain'}, 'lacto_ovo': {'status': 'suitable'}},
        'text': '推荐全聚德'}}}) == '按本次可见配料判断，纯净素：待核对；Vegan：待核对；蛋奶素：适合。'
    for result in ({}, {'results': {'vegan': {'status': 'suitable'}}},
                   {'results': {key: {'status': 'new-status'} for key in ('pure', 'vegan', 'lacto_ovo')}}):
        assert _ingredient_conclusion({'payload': {'result': result}}) == ''


def test_tool_failure_has_no_invented_shop(signed, monkeypatch):
    enable(monkeypatch)

    def boom(*a, **k):
        raise core.AppError('UPSTREAM_FAILED', '外部服务未正常返回，请稍后重试。', 502)

    monkeypatch.setattr(providers, 'request_json', boom)
    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '查到全聚德附近有几家。', 'needs_input': False, 'input_prompt': '',
                'tool_calls': []}]
    reply = _turn(signed, monkeypatch, replies)['reply']
    assert '全聚德' not in reply['text']
    assert reply['places'] == []
    assert reply['restaurant_guard']['clarification_added'] is True


def test_scope_version_is_stable_and_versioned(signed, monkeypatch):
    """准入规则带稳定版本；旧版本(v1)结果不能当作最新规则通过。"""
    enable(monkeypatch)
    fake_amap(monkeypatch, [poi('X1', '素心素食餐厅', VEG_CODE, VEG_TYPE)], count='1')
    data = signed.get('/api/v1/restaurants?city=北京').json()
    assert data['scope']['version'] == restaurant_scope.RULE_VERSION
    assert data['scope']['version'] != 'veg-admission-v1'
    assert data['items'][0]['scope']['version'] == restaurant_scope.RULE_VERSION

    replies = [{'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                                'arguments': {'city': '北京'}}]},
               {'text': '好的。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}]
    reply = _turn(signed, monkeypatch, replies)['reply']
    card = [c for c in reply['cards'] if c['type'] == 'restaurant_list'][0]
    assert card['payload']['scope']['version'] == restaurant_scope.RULE_VERSION
    # 历史读回保持同一版本标记
    msgs = signed.get('/api/v1/chat/sessions').json()['items']
    history = signed.get(f"/api/v1/chat/sessions/{msgs[0]['id']}/messages").json()['items']
    saved = [m for m in history if m['role'] == 'assistant'][0]
    assert saved['cards'][0]['payload']['scope']['version'] == restaurant_scope.RULE_VERSION
