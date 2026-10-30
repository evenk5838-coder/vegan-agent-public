"""食材草稿业务服务：创建、读取、补字段、确认。

与 HTTP 路由和 Agent `draft_foods` 工具共用；行为与既有实现一致：
- 草稿 7 天留存，确认前不创建正式食材；
- 补字段：未传=保留，显式 null 或 clear=清空，必要字段不可清空；
- 已保存项只读；确认按 `owner+draft_item_id` 去重；同 key 同内容可重放、异内容 409；
- **确认在同一 db 会话与事务内**完成食材、提醒、草稿状态与幂等结果，草稿锁与内容指纹保留。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from .. import core, drafts
from ..core import AppError, Record, now, uid
from ..schemas import DraftConfirm
from . import foods as food_service

DRAFT_KIND = 'food_draft'
OPEN_STATUSES = ('pending_confirmation', 'partially_saved')
REQUIRED_FIELDS = {'name'}
CLEARABLE_FIELDS = {'temperature_c', 'remind_at', 'expires_at', 'package_note',
                    'deadline_source', 'location', 'condition', 'date_type'}


# ---------- 读取 ----------
def load(db, identifier: str, owner: str) -> Record:
    record = db.get(Record, identifier)
    if (not record or record.owner != owner or record.kind != DRAFT_KIND
            or (record.expires and record.expires <= now())):
        raise AppError('NOT_FOUND', '找不到这条记录。', 404)
    return record


def view(record: Record) -> dict:
    from . import food_advice
    p = record.payload
    return {'draft_id': record.id, 'status': p.get('status'),
            'created': record.created.isoformat(),
            'expires_at': record.expires.isoformat() if record.expires else None,
            'items': [drafts.draft_item_view(i) for i in p.get('items', [])],
            # 稳定摘要：确认时回传，用于“依据/内容是否变化”的复核（不含时钟）
            'advice_digest': food_advice.draft_digest(p.get('items', [])),
            'advice_version': food_advice.ADVICE_VERSION}


def list_drafts(db, owner: str) -> dict:
    rows = db.scalars(select(Record).where(Record.owner == owner, Record.kind == DRAFT_KIND)
                      .order_by(Record.created.desc())).all()
    return {'items': [{'draft_id': r.id, 'status': r.payload.get('status'),
                       'created': r.created.isoformat(),
                       'expires_at': r.expires.isoformat() if r.expires else None,
                       'item_count': len(r.payload.get('items', []))} for r in rows]}


def saved_item_ids(db, owner: str, draft_id: str) -> dict:
    """draft_item_id → food_id 的持久化映射（存在正式食材记录上，草稿过期不影响）。"""
    mapping = {}
    for food in db.scalars(select(Record).where(Record.owner == owner, Record.kind == 'food')).all():
        if food.payload.get('draft_id') == draft_id and food.payload.get('draft_item_id'):
            mapping[food.payload['draft_item_id']] = food.id
    return mapping


# ---------- 创建 ----------
def create(db, owner: str, items, chat_turn_id=None, reference_time=None, timezone=None) -> Record:
    record = drafts.build_draft_record(owner, items, chat_turn_id, reference_time, timezone)
    db.add(record)
    db.commit()
    return record


# ---------- 补字段 ----------
def update_item(db, record: Record, item_id: str, changes: dict) -> dict:
    """更新单项；调用方传入已校验的 changes（None 表示清空）。"""
    if record.payload.get('status') not in OPEN_STATUSES:
        raise AppError('DRAFT_CLOSED', '这份草稿已结束，请重新整理。', 409)
    items = record.payload.get('items', [])
    target = next((i for i in items if i.get('item_id') == item_id), None)
    if not target:
        raise AppError('NOT_FOUND', '找不到这条草稿项。', 404)
    if target.get('saved_food_id') or saved_item_ids(db, record.owner, record.id).get(item_id):
        raise AppError('ITEM_SAVED', '这条食材已保存，请在电子冰箱里修改。', 409)
    actual = dict(target.get('actual') or {})
    if 'started_at' in changes and changes['started_at'] != target.get('started_at'):
        for field in ('date_phrase', 'date_source', 'date_reference_at', 'date_timezone',
                      'time_precision', 'date_warnings', 'date_missing'):
            target.pop(field, None)
            actual.pop(field, None)
    for key, value in changes.items():
        if value is None:
            target.pop(key, None)
            actual.pop(key, None)
        else:
            target[key] = value
            actual[key] = value
    target['actual'] = actual
    record.payload = {**record.payload, 'updated': now().isoformat()}
    db.commit()
    cleared = [k for k, v in changes.items() if v is None]
    return {'draft_id': record.id, 'status': record.payload['status'],
            'item': {**drafts.draft_item_view(target), 'cleared': cleared}}


def clear_targets(supplied: set, payload: dict, clear: list) -> dict:
    """把「未传/显式 null/clear 列表」规范成 changes；必要字段拒绝清空。"""
    changes = {k: payload[k] for k in supplied if k != 'clear'}
    for field, value in changes.items():
        if value is None and field in REQUIRED_FIELDS:
            raise AppError('FIELD_REQUIRED', f'{field} 是必要字段，不能清空。', 422)
    for field in clear or []:
        if field in REQUIRED_FIELDS:
            raise AppError('FIELD_REQUIRED', f'{field} 是必要字段，不能清空。', 422)
        if field not in CLEARABLE_FIELDS and field not in drafts.ITEM_FIELDS:
            raise AppError('INVALID_FIELD', f'不支持清空字段：{field}', 422)
        changes[field] = None
    return changes


# ---------- 确认 ----------
def confirm(db, record: Record, data: DraftConfirm) -> dict:
    """确认入库：食材、提醒、草稿状态与幂等记录在同一事务内提交。

    调用方负责持有草稿锁（advisory lock）；这里不重复加锁，也不吞数据库异常。
    """
    identifier, owner = record.id, record.owner
    p = record.payload
    items = p.get('items', [])
    targets = [i for i in items if not data.item_ids or i.get('item_id') in data.item_ids]
    if not targets:
        raise AppError('NOT_FOUND', '没有可确认的草稿项。', 404)
    keys = dict(p.get('confirm_keys') or {})
    scope = drafts.fingerprint(targets)
    if data.idempotency_key:
        stored = keys.get(data.idempotency_key)
        if stored:
            if stored.get('fingerprint') != scope:
                raise AppError('IDEMPOTENCY_CONFLICT', '同一幂等键对应不同的确认内容，请更换幂等键。', 409)
            return {'draft_id': identifier, 'status': stored['status'], 'results': stored['results']}

    # 确认前复核：摘要按**整批草稿**计算（与草稿详情返回的一致），
    # 这样"只重试失败项"的请求也不会因为子集不同而误报 409。
    from . import food_advice
    current_digest = food_advice.draft_digest(items)
    expected = p.get('advice_digest')
    if expected and expected != current_digest:
        raise AppError('DRAFT_CHANGED',
                       '草稿内容或期限依据已变化，请查看更新后的建议后重新确认。', 409)

    existing = saved_item_ids(db, owner, identifier)
    results = []
    for item in targets:
        item_id = item.get('item_id')
        if item_id in existing:
            results.append({'item_id': item_id, 'status': 'saved', 'food_id': existing[item_id], 'error': None})
            item['saved_food_id'] = existing[item_id]
            continue
        try:
            payload = drafts.to_food_payload(item)
            # 确认即采用当前已核对的系统建议：把依据/期限写入正式记录
            advice = food_advice.assess(item)
            basis = advice.get('basis') or {}
            if basis.get('type') in ('package', 'rule') and advice.get('date'):
                payload.update(expires_at=advice['date'],
                               deadline_source=basis['type'],
                               needs_review=False)
                if basis.get('type') == 'rule':
                    payload.update(rule_id=basis.get('rule_id'),
                                   rule_version=basis.get('rule_version'),
                                   rule_source=basis.get('rule_source'))
            elif basis.get('type') == 'none':
                # 无系统依据：保持未知，不造日期；用户自设日期沿用草稿里的值
                if payload.get('deadline_source') != 'user':
                    payload.update(expires_at=None, deadline_source='unknown', needs_review=False)
        except AppError as e:
            results.append({'item_id': item_id, 'status': 'failed', 'food_id': None,
                            'error': {'code': e.code, 'message': e.message}})
            continue
        # 正式食材插入与提醒由 foods 服务完成；不 commit，保持在本次确认事务内。
        from . import consumption_plan
        plan = item.get('consumption_plan')
        if plan and plan.get('fact_fingerprint') == consumption_plan.fingerprint(payload):
            payload['consumption_plan'] = plan
        food = food_service.insert_food(db, owner, {**payload, 'draft_id': identifier,
                                                    'draft_item_id': item_id})
        item['saved_food_id'] = food.id
        existing[item_id] = food.id
        results.append({'item_id': item_id, 'status': 'saved', 'food_id': food.id, 'error': None})

    # 整批状态按全部草稿项与持久化映射计算，未确认/失败项仍保持 partially_saved。
    saved_ids = {**existing, **{i['item_id']: i['saved_food_id'] for i in items if i.get('saved_food_id')}}
    unsaved = [i for i in items if not saved_ids.get(i.get('item_id'))]
    status = 'partially_saved' if unsaved else 'confirmed'
    if data.idempotency_key:
        keys[data.idempotency_key] = {'draft_id': identifier, 'status': status,
                                      'results': results, 'fingerprint': scope}
    record.payload = {**p, 'items': items, 'status': status, 'confirm_keys': keys,
                      'updated': now().isoformat()}
    # 确认后顺延留存期，便于恢复与核对（与既有行为一致）。
    record.expires = now() + timedelta(days=drafts.DRAFT_TTL_DAYS)
    db.commit()
    return {'draft_id': identifier, 'status': status, 'results': results}


def remove(db, record: Record):
    """删除草稿；不影响已保存的正式食材与去重映射。"""
    db.delete(record)
    db.commit()
