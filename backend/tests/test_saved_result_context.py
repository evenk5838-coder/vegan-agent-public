"""Current server-card references keep conversation positions without stale facts."""
from sqlalchemy import select

from app import core, knowledge
from app.services import chat_reads, recipe_query


def owner():
    with core.DB() as db:
        return db.scalar(select(core.User)).id


def recipe(client, title):
    response = client.post('/api/v1/recipes', json={
        'title': title, 'ingredients': '豆腐、番茄', 'steps': '切块后炒熟。',
        'meal': 'main', 'cuisine': 'chinese', 'confirmed': True})
    assert response.status_code == 201, response.text
    return response.json()


def card(kind, items):
    return {'type': kind, 'payload': {'items': items}}


def test_recipe_positions_survive_deletion_and_title_is_refreshed(signed):
    first, second = recipe(signed, '第一道'), recipe(signed, '第二道')
    old = card('recipe_list', [first, second])
    with core.DB() as db:
        db.delete(db.get(core.Record, first['id']))
        row = db.get(core.Record, second['id'])
        row.payload = {**row.payload, 'title': '第二道新名称'}
        db.commit()
    result = chat_reads.SavedResultContext(owner(), {}).for_cards([old])[0]
    assert result == {'type': 'recipe_list', 'total_items': 2, 'truncated': False,
                      'items': [{'position': 1, 'unavailable': True},
                                {'position': 2, 'recipe_id': second['id'], 'title': '第二道新名称'}]}
    assert 'steps' not in str(result) and 'ingredients' not in str(result)


def test_private_recipe_and_food_never_cross_accounts(signed):
    private = recipe(signed, '私人菜谱不能泄露')
    food = signed.post('/api/v1/foods', json={'name': '私人食材不能泄露'}).json()
    context = chat_reads.SavedResultContext('another-owner', {})
    result = context.for_cards([card('recipe_list', [private]), card('food_inventory', [food])])
    assert all(row['items'] == [{'position': 1, 'unavailable': True}] for row in result)
    assert private['id'] not in str(result) and food['id'] not in str(result)
    assert '不能泄露' not in str(result)


def test_archived_public_recipe_no_longer_enters_context(signed):
    knowledge.import_entries([{'type': 'recipe', 'title': '公共豆腐',
                              'source': 'https://example.org/synthetic-recipe',
                              'ingredients': ['豆腐'], 'steps': ['炒熟'], 'body': '',
                              'conditions': {'cuisine': 'chinese'}}])
    with core.DB() as db:
        record = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
        identifier = record.payload['entry_id']
    knowledge.set_status(identifier, 'published')
    old = card('recipe_list', [{'entry_id': identifier, 'title': '公共豆腐'}])
    assert chat_reads.SavedResultContext(owner(), {}).for_cards([old])[0]['items'][0] == {
        'position': 1, 'recipe_id': identifier, 'title': '公共豆腐'}
    knowledge.set_status(identifier, 'archived')
    assert chat_reads.SavedResultContext(owner(), {}).for_cards([old])[0]['items'] == [
        {'position': 1, 'unavailable': True}]


def test_food_detail_refreshes_current_storage_status_and_version(signed):
    food = signed.post('/api/v1/foods', json={'name': '豆腐', 'storage': 'ambient'}).json()
    with core.DB() as db:
        row = db.get(core.Record, food['id'])
        row.payload = {**row.payload, 'storage': 'frozen', 'status': 'used', 'version': 'current-v2',
                       'notes': 'not model context', 'consumption_plan': {'internal': 'never forward'}}
        db.commit()
    result = chat_reads.SavedResultContext(owner(), {}).for_cards([{'type': 'food_detail', 'payload': food}])
    assert result[0]['items'] == [{'position': 1, 'food_id': food['id'], 'name': '豆腐',
                                  'storage': 'frozen', 'status': 'used', 'version': 'current-v2'}]
    assert 'notes' not in str(result) and 'consumption_plan' not in str(result)


def test_projection_is_bounded_retains_order_and_loads_maps_once(signed, monkeypatch):
    uid = owner()
    rows = [{'id': f'recipe-{number}', 'title': f'菜谱{number}', 'steps': 'not forwarded'} for number in range(25)]
    calls = []
    def query(current_owner, filters, preferences):
        calls.append(current_owner)
        return {'items': rows}
    monkeypatch.setattr(recipe_query, 'query_recipes', query)
    context = chat_reads.SavedResultContext(uid, {}, max_items=1000)
    result = context.for_cards([card('recipe_list', rows)])
    assert len(result[0]['items']) == 20 and result[0]['total_items'] == 25 and result[0]['truncated'] is True
    assert [item['position'] for item in result[0]['items']] == list(range(1, 21))
    assert [item['recipe_id'] for item in result[0]['items']] == [row['id'] for row in rows[:20]]
    detail = context.for_cards([{'type': 'recipe_detail', 'payload': rows[0]}])
    assert detail[0]['items'][0]['recipe_id'] == rows[0]['id']
    assert calls == [uid]
    assert 'steps' not in str(result)


def test_malformed_and_unsupported_cards_do_not_expand_context(signed):
    context = chat_reads.SavedResultContext(owner(), {})
    assert context.for_cards(None) == []
    assert context.for_cards([None, {'type': 'recipe_list', 'payload': []},
                              card('unknown', [{'id': 'ignored'}])]) == []
    result = context.for_cards([card('recipe_list', [None, {}, {'title': 'no ID'}])] * 8)
    assert len(result) == 4 and result[-1]['additional_cards_omitted'] == 4
    assert result[0]['items'] == [{'position': position, 'unavailable': True} for position in (1, 2, 3)]
