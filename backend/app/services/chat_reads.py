"""对话读取与查询工具：让 Agent 能读真实账号数据，而不是靠聊天记忆。

本模块只做**读**（外加经确认的提醒设置），写操作仍由确认卡片/页面完成。
所有查询都按 owner 过滤，返回结构与 HTTP 端点一致（同一服务）。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from .. import core, knowledge
from ..core import AppError, Record, now
if True:  # 直接引用具体模块，避免经过 services/__init__ 造成循环导入
    from . import food_drafts as draft_service
    from . import foods as food_service
    from . import recipe_query


def ingredient_history(owner: str, limit: int = 10) -> dict:
    """往期配料核对（只读；不含原图二进制）。"""
    with core.DB() as db:
        rows = db.scalars(select(Record).where(Record.owner == owner, Record.kind == 'ingredient',
                                               Record.expires > now())
                          .order_by(Record.created.desc()).limit(max(1, min(limit, 20)))).all()
        items = []
        for row in rows:
            photo = (row.payload.get('photo') or {})
            items.append({'history_id': row.id, 'created': row.created.isoformat(),
                          'text': row.payload.get('text'),
                          'results': {k: v.get('status') for k, v in
                                      (row.payload.get('result') or {}).get('results', {}).items()},
                          'pungent': (row.payload.get('result') or {}).get('pungent'),
                          'saved_original': bool(photo.get('saved_original')),
                          'asset_id': photo.get('asset_id')})
    return {'items': items, 'count': len(items)}


def food_inventory(owner: str, expiring_days: int | None = None, include_inactive: bool = False) -> dict:
    """库存列表；`expiring_days` 给定时只返回该天数内到期或已过期的。"""
    with core.DB() as db:
        items = food_service.read_all(db, owner)
    if not include_inactive:
        items = [i for i in items if i.get('status') == 'active']
    if expiring_days is not None:
        threshold = now() + timedelta(days=max(0, min(expiring_days, 90)))
        kept = []
        for item in items:
            deadline = item.get('valid_deadline_at')
            if not deadline:
                continue
            if deadline <= threshold.isoformat():
                kept.append(item)
        items = kept
    return {'items': items, 'count': len(items),
            'notice': '依据包装或已发布规则；无依据的食材不显示系统期限。'}


def food_detail(owner: str, food_id: str) -> dict:
    with core.DB() as db:
        record = food_service.load(db, food_id, owner)
        return food_service.view(record)


def reminder_status(owner: str) -> dict:
    """提醒任务与本轮关联的食材名称（不暴露其他账号）。"""
    with core.DB() as db:
        names = {r.id: r.payload.get('name') for r in
                 db.scalars(select(Record).where(Record.owner == owner, Record.kind == 'food'))}
        jobs = db.scalars(select(core.Job).where(core.Job.owner == owner, core.Job.kind == 'reminder')
                          .order_by(core.Job.due)).all()
        items = [{'task_id': j.id, 'status': j.status, 'due': j.due.isoformat(),
                  'food_id': j.payload.get('food_id'),
                  'food_name': names.get(j.payload.get('food_id')),
                  'result': j.result or {}} for j in jobs[-20:]]
    return {'items': items, 'count': len(items)}


def recipe_detail(owner: str, preferences: dict, recipe_id: str | None = None,
                  title: str | None = None) -> dict:
    """按 ID 或名称定位菜谱详情；个人与公共统一 DTO。"""
    rows = recipe_query.query_recipes(owner, {}, preferences)['items']
    target = None
    if recipe_id:
        target = next((r for r in rows if r.get('id') == recipe_id or r.get('entry_id') == recipe_id), None)
    elif title:
        needle = title.strip()
        exact = [r for r in rows if (r.get('title') or '').strip() == needle]
        target = exact[0] if len(exact) == 1 else None
        if target is None:
            fuzzy = [r for r in rows if needle and needle in (r.get('title') or '')]
            target = fuzzy[0] if len(fuzzy) == 1 else None
    if target is None:
        raise AppError('NOT_FOUND', '没有找到唯一匹配的菜谱，请提供更完整的菜名。', 404)
    return target


class SavedResultContext:
    """Small current entity references for server-saved conversation cards.

    Create once per turn, then pass only cards from that owner's unexpired
    assistant messages in the current session. Message/session visibility is
    the caller's responsibility; entity visibility is checked here. Original
    list positions are retained even when a record has since disappeared.
    No tutorial, source body, advice or importer metadata enters this context.
    """

    MAX_ITEMS = 20
    MAX_CARDS = 4
    SUPPORTED = {'recipe_list', 'recipe_detail', 'food_inventory', 'food_detail'}

    def __init__(self, owner: str, preferences: dict, max_items: int = 20):
        self.owner = owner
        self.preferences = preferences
        self.max_items = max(1, min(int(max_items), self.MAX_ITEMS))
        self._recipe_map = None
        self._food_map = None

    def _recipes(self) -> dict:
        if self._recipe_map is None:
            rows = recipe_query.query_recipes(self.owner, {}, self.preferences)['items']
            self._recipe_map = {row['id']: row for row in rows if isinstance(row.get('id'), str)}
        return self._recipe_map

    def _foods(self) -> dict:
        if self._food_map is None:
            # Only the minimal fields are projected below. Avoid computing
            # unrelated advice/progress or loading another owner's inventory.
            with core.DB() as db:
                rows = db.scalars(select(Record).where(Record.owner == self.owner,
                                                       Record.kind == food_service.FOOD_KIND))
                self._food_map = {row.id: {'id': row.id, **row.payload} for row in rows}
        return self._food_map

    def _reference(self, kind: str, item, position: int) -> dict:
        missing = {'position': position, 'unavailable': True}
        if not isinstance(item, dict):
            return missing
        identifier = item.get('id') or (item.get('entry_id') if kind.startswith('recipe_') else None)
        if not isinstance(identifier, str):
            return missing
        is_recipe = kind.startswith('recipe_')
        current = (self._recipes() if is_recipe else self._foods()).get(identifier)
        if current is None:
            return missing
        if is_recipe:
            return {'position': position, 'recipe_id': identifier,
                    'title': str(current.get('title') or '')[:200]}
        return {'position': position, 'food_id': identifier,
                'name': str(current.get('name') or '')[:200],
                'storage': current.get('storage'), 'status': current.get('status'),
                'version': current.get('version')}

    def for_cards(self, cards: list[dict]) -> list[dict]:
        """Return bounded data, retaining each list's original 1-based order."""
        if not isinstance(cards, list):
            return []
        recognized = [card for card in cards if isinstance(card, dict)
                      and card.get('type') in self.SUPPORTED
                      and isinstance(card.get('payload'), dict)]
        result = []
        for card in recognized[:self.MAX_CARDS]:
            kind, payload = card['type'], card['payload']
            if kind in ('recipe_list', 'food_inventory'):
                rows = payload.get('items')
                rows = rows if isinstance(rows, list) else []
            else:
                rows = [payload]
            result.append({'type': kind,
                           'items': [self._reference(kind, item, position)
                                     for position, item in enumerate(rows[:self.max_items], 1)],
                           'total_items': len(rows), 'truncated': len(rows) > self.max_items})
        if result and len(recognized) > self.MAX_CARDS:
            result[-1]['additional_cards_omitted'] = len(recognized) - self.MAX_CARDS
        return result


def recipe_candidates(owner: str) -> dict:
    """未保存的联网候选（跨设备可恢复）。"""
    with core.DB() as db:
        rows = db.scalars(select(Record).where(Record.owner == owner, Record.kind == 'candidate')).all()
        items = []
        for row in rows:
            if row.expires and row.expires <= now():
                continue
            items.append({'candidate_id': row.id, 'title': row.payload.get('title'),
                          'source_url': row.payload.get('source_url'),
                          'summary': row.payload.get('summary'),
                          'status': row.payload.get('status'),
                          'saved_recipe_id': row.payload.get('saved_recipe_id'),
                          'tutorial_complete': False,
                          'expires_at': row.expires.isoformat() if row.expires else None})
    return {'items': items, 'count': len(items)}


def my_drafts(owner: str) -> dict:
    with core.DB() as db:
        data = draft_service.list_drafts(db, owner)
        drafts = []
        for summary in data['items'][:10]:
            record = draft_service.load(db, summary['draft_id'], owner)
            drafts.append(draft_service.view(record))
    return {'items': drafts, 'count': len(drafts)}


def _knowledge_source_notice(item: dict) -> str:
    """Preserve source limits in prose without forwarding importer/rule caches.

    Published availability is not a professional or cooking review. These
    notices describe recorded limitations only; an absent review flag must not
    be promoted to either an approval or a claim that a review took place.
    """
    conditions = item.get('conditions') or {}
    source_meta = item.get('source_meta') or {}
    metadata = {**(conditions if isinstance(conditions, dict) else {}),
                **(source_meta if isinstance(source_meta, dict) else {})}
    notices = []
    if metadata.get('manual_reviewed') is False:
        notices.append('此来源尚未经过人工审核；公开可检索不代表已通过专业审核或试做验证。')
    translation = metadata.get('translation') or {}
    translation = translation if isinstance(translation, dict) else {}
    validation = translation.get('validation') or {}
    validation = validation if isinstance(validation, dict) else {}
    if ('machine_translation_unreviewed' in (metadata.get('translation_quality_flags') or [])
            or (translation and validation.get('semantic_quality_manually_reviewed') is False)):
        notices.append('本文包含尚未人工核对的机器翻译。')
    labels = metadata.get('machine_diet_labels') or {}
    if any(isinstance(label, dict)
           and (label.get('status') == 'uncertain' or label.get('unknown')
                or label.get('incomplete')) for label in
           (labels.values() if isinstance(labels, dict) else [])):
        notices.append('部分原料的适配信息仍待核对，不能据此确认符合某一素食类别。')
    if 'missing_servings' in (metadata.get('quality_flags') or []):
        notices.append('来源未提供份量信息。')
    return ''.join(notices)


def knowledge_search(query: str, kind: str | None = None, limit: int = 5) -> dict:
    """检索**已发布**知识条目（菜谱/营养/储存/配料规则），返回可引用的 entry_id 与来源。"""
    data = knowledge.public_items(kind)
    items = []
    # Page and Agent retrieval share ingredients, cooking steps, source and
    # applicability extraction/matching. Nonrecipe body/nutrition stays searchable.
    for item in recipe_query._match_query(data['items'], query):
        conditions = item.get('conditions') or {}
        # Tool consumers need applicability facts and citation identity, not
        # importer IDs, revision/hash pins, review keys or cached parse tokens.
        public_conditions = {field: conditions[field] for field in recipe_query.CONDITION_SEARCH_FIELDS
                             if isinstance(conditions, dict) and field in conditions}
        items.append({'entry_id': item['entry_id'], 'type': item['type'], 'title': item['title'],
                      'source': item['source'], 'conditions': public_conditions,
                      'source_notice': _knowledge_source_notice(item),
                      'excerpt': recipe_query.search_excerpt(item, query)})
    items = items[:max(1, min(limit, 10))]
    return {'items': items, 'count': len(items), 'state': data['state'],
            'notice': data['notice'] if not items else '仅返回已发布条目，引用时请标注来源。'}
