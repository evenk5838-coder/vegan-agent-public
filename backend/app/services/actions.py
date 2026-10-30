"""持久化确认操作（pending action）。

用途：任何**正式修改**（改提醒、标用完/丢弃、改存放条件并重评）都先落一条待确认操作，
由用户在卡片上确认后，服务端才执行。模型不能自己替用户确认。

保证：
- 目标与拟改值持久化，刷新/换设备/重启后可恢复；
- 记录 `base_version`（目标对象当时的版本），执行时版本不符 → `409 ACTION_STALE`；
- 幂等：同 key 重复确认返回首次结果；重复创建同一 pending 内容不重复入队；
- 取消、过期（默认 30 分钟）有明确状态；删除仍只在页面（本服务不含删除动作）。
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from .. import core
from ..core import AppError, Record, advisory_lock, digest, now, uid

KIND = 'pending_action'
TTL_MINUTES = 30
OPEN_STATUS = 'pending'
ALLOWED_ACTIONS = {'food_status', 'food_condition', 'food_reminder'}
CONTEXT_ACK_FIELDS = {'started_at', 'date_type', 'storage_note', 'package_note'}


def _payload(record: Record) -> dict:
    return record.payload


def create(db, owner: str, kind: str, target: dict, proposed: dict,
           base_version: str | None, chat_turn_id: str | None = None) -> dict:
    """创建待确认操作。同一目标 + 同一拟改值 + 仍 pending 时复用，不重复入队。"""
    if kind != 'food_action':
        raise AppError('INVALID_ARGUMENTS', f'不支持的操作类型：{kind}', 422)
    action = (proposed.get('action') or '').strip()
    if action not in ALLOWED_ACTIONS:
        raise AppError('INVALID_ARGUMENTS', f'不支持的修改类型：{action}', 422)
    if action != 'food_condition' and CONTEXT_ACK_FIELDS & set(proposed):
        raise AppError('INVALID_ARGUMENTS', '补充日期和储存信息请使用食材情况更新。', 422)
    fingerprint = digest(json.dumps(
        {'food_id': target.get('food_id'), 'action': action,
         'proposed': {k: proposed.get(k) for k in
                      ('status', 'storage', 'condition', 'temperature_c', 'remind_at',
                       'opened', 'food_class', 'started_at', 'date_type', 'storage_note', 'package_note',
                       '_consumption_plan')},
         'base_version': base_version},
        ensure_ascii=False, sort_keys=True))
    # Serialize the read-and-create pair, not only later confirmations. This
    # lock is transaction scoped and the same fingerprint is owner scoped.
    advisory_lock(db, 'action-create', owner, fingerprint)
    for record in db.scalars(select(Record).where(Record.owner == owner, Record.kind == KIND,
                                                  Record.payload['status'].as_string() == OPEN_STATUS)):
        if (record.payload.get('fingerprint') == fingerprint
                and (not record.expires or record.expires > now())):
            return view(record)
    record = Record(owner=owner, kind=KIND,
                    payload={'action_id': uid(), 'fingerprint': fingerprint, 'kind': kind,
                             'target': target, 'proposed': proposed,
                             'base_version': base_version, 'chat_turn_id': chat_turn_id,
                             'status': OPEN_STATUS, 'results': None, 'confirm_key': None,
                             'created_at': now().isoformat()},
                    expires=now() + timedelta(minutes=TTL_MINUTES))
    db.add(record)
    db.commit()
    return view(record)


def view(record: Record) -> dict:
    p = _payload(record)
    # Expiry itself is persistent. Derive its effective read state without a
    # GET overwriting a concurrently committed confirmation or cancellation.
    status = p.get('status')
    if status == OPEN_STATUS and record.expires and record.expires <= now():
        status = 'expired'
    return {'action_id': p.get('action_id'), 'record_id': record.id,
            'status': status, 'kind': p.get('kind'),
            'target': p.get('target'), 'proposed': p.get('proposed'),
            'base_version': p.get('base_version'),
            'created_at': p.get('created_at'),
            'expires_at': record.expires.isoformat() if record.expires else None,
            'results': p.get('results'),
            'needs_confirmation': status == OPEN_STATUS,
            'required_ack_fields': sorted(CONTEXT_ACK_FIELDS & set(p.get('proposed') or {}))}


def load(db, record_id: str, owner: str) -> Record:
    record = db.get(Record, record_id)
    if not record or record.owner != owner or record.kind != KIND:
        raise AppError('NOT_FOUND', '找不到这项待确认操作。', 404)
    return record


def list_open(db, owner: str) -> dict:
    rows = db.scalars(select(Record).where(Record.owner == owner, Record.kind == KIND)
                      .order_by(Record.created.desc())
                      .execution_options(populate_existing=True)).all()
    items = []
    for record in rows:
        items.append(view(record))
    return {'items': items, 'count': len(items)}


def cancel(db, record: Record, owner: str | None = None) -> dict:
    owner = owner or record.owner
    _lock(db, owner, (record.payload.get('target') or {}).get('food_id'), record.id)
    db.refresh(record)
    if record.payload.get('status') != OPEN_STATUS:
        raise AppError('ACTION_CLOSED', '这项操作已结束，不能取消。', 409)
    if record.expires and record.expires <= now():
        record.payload = {**record.payload, 'status': 'expired'}
        db.commit()
        raise AppError('ACTION_EXPIRED', '这项操作已过期，请重新发起。', 409)
    record.payload = {**record.payload, 'status': 'cancelled'}
    db.commit()
    return view(record)


def _lock(db, owner: str, food_id: str | None, action_record_id: str):
    """统一锁顺序：先 food 再 action，避免确认/取消/目标更新互相死锁。"""
    if food_id:
        advisory_lock(db, 'food', owner, food_id)
    advisory_lock(db, 'action', owner, action_record_id)


def _execute(db, owner: str, p: dict) -> dict:
    """执行拟改值：统一走 foods 共用服务，保留历史、重评与提醒同步。"""
    from . import food_service
    food_id = (p.get('target') or {}).get('food_id')
    record = food_service.load(db, food_id, owner)
    if p.get('base_version') and record.payload.get('version') != p.get('base_version'):
        raise AppError('ACTION_STALE', '这条食材在你确认前已被修改，请刷新后重新确认。', 409)
    proposed = p.get('proposed') or {}
    action = proposed.get('action')

    # 组装成 FoodInput 等价载荷：以当前记录为基线，只覆盖拟改值
    payload = {k: v for k, v in record.payload.items()
               if k not in ('storage_history', 'version', 'needs_review')}
    supplied = set()
    if action == 'food_status':
        status = proposed.get('status')
        if status not in ('active', 'used', 'discarded'):
            raise AppError('INVALID_ARGUMENTS', '状态只能是 active/used/discarded。', 422)
        payload['status'] = status
        supplied.add('status')
    elif action in ('food_condition', 'food_reminder'):
        for key in ('storage', 'condition', 'temperature_c', 'location',
                    'food_class', 'opened', 'remind_at', 'started_at', 'date_type', 'storage_note', 'package_note'):
            if key in proposed and proposed.get(key) not in (None, ''):
                payload[key] = proposed[key]
                supplied.add(key)
        if action == 'food_reminder' and not proposed.get('remind_at'):
            payload.pop('remind_at', None)
            supplied.add('remind_at')          # 显式清空提醒
    else:
        raise AppError('INVALID_ARGUMENTS', f'不支持的修改类型：{action}', 422)

    # 统一由 foods.update 处理：写历史、自动重评、撤销/重排提醒、递增版本
    # commit=False：目标更新与确认终态在同一事务内提交
    # 锁已由 confirm 的 lock 会话按 food→action 顺序持有，这里不再重复取锁（会自锁）
    food_service.update(db, record, payload, supplied, reconfirmed=False,
                        commit=False, lock=False, context_confirmed=bool(CONTEXT_ACK_FIELDS & set(proposed)),
                        proposed_plan=proposed.get('_consumption_plan'))
    if action == 'food_status':
        record.payload = {**record.payload, 'version': uid()}
    db.flush()
    return {'food_id': food_id, 'action': action, 'result': food_service.view(record)}


def confirm(db, record: Record, owner: str, confirm_key: str | None, idempotency_key: str | None, acknowledged_fields=None) -> dict:
    """用户确认后执行。

    并发安全：用**独立会话持有事务级 advisory lock**（先 food 后 action），
    执行与终态在另一个会话的同一事务内提交；拿锁后重读最新状态，
    因此并发确认只会执行一次，另一次返回重放或明确的冲突。
    """
    record_id = record.id
    required = CONTEXT_ACK_FIELDS & set(record.payload.get('proposed') or {})
    if required - set(acknowledged_fields or []):
        raise AppError('CONTEXT_CONFIRMATION_REQUIRED', '请先核对本次补充的日期和储存信息，再确认更新。', 422)
    food_id = (record.payload.get('target') or {}).get('food_id')

    with core.DB() as lock_db:
        _lock(lock_db, owner, food_id, record_id)
        with core.DB() as work:
            fresh = work.get(Record, record_id)
            if not fresh or fresh.owner != owner or fresh.kind != KIND:
                raise AppError('NOT_FOUND', '找不到这项待确认操作。', 404)
            p = dict(fresh.payload)
            if p.get('status') == 'confirmed':
                return {**view(fresh), 'replayed': True}
            if p.get('status') != OPEN_STATUS:
                raise AppError('ACTION_CLOSED', f'这项操作已结束（{p.get("status")}）。', 409)
            if fresh.expires and fresh.expires <= now():
                fresh.payload = {**p, 'status': 'expired'}
                work.commit()
                raise AppError('ACTION_EXPIRED', '这项操作已过期，请重新发起。', 409)
            if confirm_key != 'user_confirmed':
                raise AppError('CONFIRMATION_REQUIRED', '需要用户明确确认后才能执行。', 400)
            results = _execute(work, owner, p)
            fresh.payload = {**p, 'status': 'confirmed', 'results': results,
                             'confirm_key': idempotency_key or p.get('confirm_key')}
            work.commit()
            return {**view(fresh), 'replayed': False}
