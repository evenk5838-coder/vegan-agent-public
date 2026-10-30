"""正式食材业务服务：读取、保存、状态更新、期限重评、进度与提醒。

职责边界（架构说明 v0.3 第 8 节）：
- HTTP 路由与草稿确认都调用这里，不各写一份；
- 不导入 main.py、不调用本机 HTTP 端点；
- `insert_food()` 接受调用方的 db 会话且**不自行 commit**，以便草稿确认在同一事务内完成；
  其余对外操作用自己的事务并在结束时提交（重评 409 需先提交待核对状态，见 `reevaluate`）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from .. import core, storage_rules
from ..core import AppError, Job, Record, now, uid
from ..schemas import ReevaluateInput

FOOD_KIND = 'food'
REMINDER_OPEN_STATUSES = ('pending', 'waiting_configuration', 'waiting_subscription')


# ---------- 读取 ----------
def load(db, identifier: str, owner: str) -> Record:
    """按账号读取单条食材；不存在或越权统一 404。"""
    record = db.get(Record, identifier)
    if not record or record.owner != owner or record.kind != FOOD_KIND:
        raise AppError('NOT_FOUND', '找不到这条记录。', 404)
    return record


def read_all(db, owner: str) -> list[dict]:
    """本人食材列表（创建时间倒序），附派生状态与进度。"""
    rows = db.scalars(select(Record).where(Record.owner == owner, Record.kind == FOOD_KIND)
                      .order_by(Record.created.desc()))
    from . import food_advice, consumption_plan
    items = []
    for record in rows:
        item = {'id': record.id, 'created': record.created.isoformat(), **record.payload}
        item['display_status'] = status(record.payload)
        item.update(progress(record.payload))
        item['suggestion'] = food_advice.suggestion_view(record.payload)
        item['consumption_plan'] = consumption_plan.view(record.payload)
        items.append(item)
    return items


def view(record: Record) -> dict:
    """单条返回结构：记录字段 + 派生状态与进度 + 建议（与列表一致）。"""
    from . import food_advice, consumption_plan
    payload = record.payload
    return {'id': record.id, 'created': record.created.isoformat(), **payload,
            'display_status': status(payload), **progress(payload),
            'suggestion': food_advice.suggestion_view(payload),
            'consumption_plan': consumption_plan.view(payload)}


# ---------- 进度与状态（纯函数） ----------
def known_storage_context(p: dict) -> bool:
    """Only recorded facts qualify; Record.created is never a food date."""
    if (p.get('storage') not in ('ambient', 'refrigerated', 'frozen')
            or p.get('condition') not in ('raw', 'cut', 'cooked', 'opened', 'unopened')
            or p.get('date_type') not in ('purchase', 'production', 'prepared', 'opened')):
        return False
    try:
        started = datetime.fromisoformat(str(p.get('started_at') or ''))
        return started.tzinfo is not None and started.utcoffset() is not None
    except (ValueError, TypeError):
        return False


def progress(p: dict) -> dict:
    """派生进度结构（计算值，不落库）：条件与期限依据有效时才显示日期进度。"""
    started = p.get('started_at')
    needs_review = bool(p.get('needs_review'))
    deadline = None if needs_review else p.get('expires_at')
    source = p.get('deadline_source', 'unknown')
    if ((source == 'rule' and not _current_rule_basis(p))
            or (source == 'package' and not _current_package_basis(p))):
        # 历史的宽泛、品质或旧版规则日期不得继续伪装成当前正式依据。
        source, deadline, needs_review = 'unknown', None, True
    if not known_storage_context(p) or source not in ('package', 'rule', 'user'):
        source, deadline = 'unknown', None
    basis_type = 'none'
    if deadline:
        basis_type = ('rule' if source == 'rule' else 'user' if source == 'user'
                      else 'package' if source == 'package' else 'none')
    valid_started = datetime.fromisoformat(started) if started and source in ('package', 'rule') else None
    stored_days = round((now() - valid_started).total_seconds() / 86400, 1) if valid_started else None
    remaining_days = (round((datetime.fromisoformat(deadline) - now()).total_seconds() / 86400, 1)
                      if deadline else None)
    if needs_review:
        assessment = 'needs_review'
    elif source == 'user':
        assessment = 'user_set'
    elif basis_type in ('package', 'rule'):
        assessment = 'estimated'
    else:
        assessment = 'unknown'
    return {'valid_started_at': valid_started.isoformat() if valid_started else None,
            'valid_deadline_at': deadline,
            'stored_for_days': stored_days,
            'date_origin': p.get('date_type'),
            'elapsed_time_label': '制作后经过' if p.get('date_type') == 'prepared' else '依据起点经过',
            'estimated_remaining_days': remaining_days,
            'assessment_status': assessment,
            'needs_review': needs_review,
            'deadline_basis': {'type': basis_type,
                               'rule_id': p.get('rule_id'),
                               'rule_version': p.get('rule_version'),
                               'rule_source': p.get('rule_source'),
                               'rules_status': p.get('rules_status'),
                               'note': p.get('package_note') or p.get('deadline_note') or ''}}


def status(p: dict) -> str:
    if p.get('status') != 'active':
        return p['status']
    if (p.get('needs_review')
            or (p.get('deadline_source') == 'rule' and not _current_rule_basis(p))
            or (p.get('deadline_source') == 'package' and not _current_package_basis(p))):
        return 'needs_review'
    if not known_storage_context(p) or p.get('deadline_source') not in ('package', 'rule', 'user'):
        return 'unknown'
    if p.get('expires_at') and datetime.fromisoformat(p['expires_at']) <= now():
        return 'expired'
    if p.get('remind_at') and datetime.fromisoformat(p['remind_at']) <= now():
        return 'due'
    return 'unknown' if not p.get('expires_at') else 'stored'


def _current_rule_basis(payload: dict) -> bool:
    """Read-only validation; invalid historical rule dates are not effective dates."""
    if not payload.get('rule_id') or payload.get('rule_version') != storage_rules.RULES_VERSION:
        return False
    rule, deadline, _ = storage_rules.estimate_deadline(payload, payload.get('rule_id'), published_only=True)
    if not rule or not deadline:
        return False
    try:
        return datetime.fromisoformat(str(payload.get('expires_at'))) == deadline
    except (TypeError, ValueError):
        return False


def _current_package_basis(payload: dict) -> bool:
    """Current package conditions, shared by reads, writes and reminder guards."""
    from . import food_advice
    valid, _ = food_advice.package_applicable(payload)
    return valid


# ---------- 提醒（与食材写入同事务） ----------
def cancel_reminders(db, food_id: str, owner: str):
    """取消该食材尚未发送的提醒任务（已发送的不追回）。"""
    for job in db.scalars(select(Job).where(Job.owner == owner, Job.kind == 'reminder',
                                            Job.status.in_(REMINDER_OPEN_STATUSES))):
        if job.payload.get('food_id') == food_id:
            job.status = 'cancelled'


def plan_reminder(db, record: Record):
    """Only an explicitly selected reminder time schedules a delivery."""
    p = record.payload
    basis = reminder_basis(p)
    if not basis or not p.get('remind_at'):
        return
    due = datetime.fromisoformat(p['remind_at'])
    if due > datetime.fromisoformat(basis['target_at']):
        return
    # Editing the record must not send the same already-accepted reminder twice.
    for previous in db.scalars(select(Job).where(Job.owner == record.owner, Job.kind == 'reminder',
                                                 Job.status == 'accepted')):
        original_due = previous.payload.get('remind_at')
        original_due = datetime.fromisoformat(original_due) if original_due else previous.due
        if previous.payload.get('food_id') == record.id and original_due == due:
            return
    db.add(Job(owner=record.owner, kind='reminder', due=due,
               payload={'food_id': record.id, 'version': p['version'],
                        'remind_at': p['remind_at'], 'reminder_basis': basis}))


def reminder_basis(payload: dict) -> dict | None:
    """Current date or separate verified eating plan; never turn a plan into expiry."""
    if payload.get('status') != 'active' or not known_storage_context(payload):
        return None
    source, expiry = payload.get('deadline_source') or 'unknown', payload.get('expires_at')
    if expiry or source != 'unknown':
        if (not expiry or source not in ('package', 'rule', 'user') or payload.get('needs_review')
                or (source == 'rule' and not _current_rule_basis(payload))
                or (source == 'package' and not _current_package_basis(payload))):
            return None
        try:
            target = datetime.fromisoformat(expiry)
            if target.tzinfo is None or target.utcoffset() is None:
                return None
        except (TypeError, ValueError):
            return None
        return {'kind': 'deadline', 'target_at': target.isoformat(), 'deadline_source': source}
    from . import consumption_plan
    plan = consumption_plan.view(payload)
    if (plan and plan.get('scope') == 'eating_plan' and plan.get('status') in ('ready', 'due')
            and plan.get('basis') in ('web_reference', 'conservative_schedule')):
        # needs_review governs the formal expiry. A new source-backed plan can
        # stand on its own only when its fingerprint matches the current facts.
        return {'kind': 'eating_plan', 'target_at': plan['target_at'],
                'plan_fingerprint': payload['consumption_plan']['fact_fingerprint']}
    return None


def validate_reminder(payload: dict):
    """Reject a new reminder that would otherwise be saved without a real task."""
    if not payload.get('remind_at') or payload.get('status') != 'active':
        return
    basis = reminder_basis(payload)
    if not basis:
        raise AppError('REMINDER_BASIS_REQUIRED', '还没有适用的食用安排或日期，请先核对后再设置提醒。', 422)
    if datetime.fromisoformat(payload['remind_at']) > datetime.fromisoformat(basis['target_at']):
        raise AppError('REMINDER_AFTER_TARGET', '提醒时间不能晚于当前食用安排或已设日期，请选择更早的时间。', 422)


def sync_reminders(db, record: Record):
    cancel_reminders(db, record.id, record.owner)
    plan_reminder(db, record)


# ---------- 写入 ----------
def insert_food(db, owner: str, payload: dict) -> Record:
    """插入正式食材并排提醒。**不 commit**，由调用方决定事务边界。"""
    payload = {**payload, 'version': uid(), 'storage_history': payload.get('storage_history', []),
               'needs_review': payload.get('needs_review', False)}
    if payload.get('deadline_source') == 'package' and not _current_package_basis(payload):
        payload['needs_review'] = True
    validate_reminder(payload)
    record = Record(owner=owner, kind=FOOD_KIND, payload=payload)
    db.add(record)
    db.flush()
    plan_reminder(db, record)
    return record


def create(db, owner: str, payload: dict) -> Record:
    record = insert_food(db, owner, payload)
    db.commit()
    return record


def update(db, record: Record, payload: dict, supplied_fields: set[str], reconfirmed: bool,
           commit: bool = True, lock: bool = True, context_confirmed: bool = False,
           proposed_plan: dict | None = None) -> Record:
    """lock=True 时先取食材级 advisory lock：与确认操作、重评、删除互斥。"""
    """状态/字段更新：保留历史、未传=保留、显式 null=清空，并同步提醒。"""
    if lock:
        core.advisory_lock(db, 'food', record.owner, record.id)
        db.refresh(record)
    old = record.payload
    p = payload
    if old.get('consumption_plan'):
        p['consumption_plan'] = old['consumption_plan']
    # 规则依据只能由重评接口生成：这里只允许保留原依据，不允许客户端新造或替换。
    if p.get('deadline_source') == 'rule':
        if old.get('deadline_source') != 'rule' or not old.get('rule_id'):
            raise AppError('INVALID_RULE_BASIS',
                           '规则期限只能由重评接口按适用规则生成，请改用手动日期或先重新核对。', 422)
        if p.get('expires_at') and old.get('expires_at'):
            try:
                same = (datetime.fromisoformat(str(p['expires_at'])) ==
                        datetime.fromisoformat(str(old['expires_at'])))
            except ValueError:
                same = False
            if not same:
                raise AppError('INVALID_RULE_BASIS', '规则期限不能通过状态修改直接改日期，请重新核对。', 422)
    # 未传的可选字段=保留旧值；显式传 null=清空（如清掉温度、提醒、自设日期）。
    for key in ('storage', 'condition', 'date_type', 'started_at', 'location',
                'temperature_c', 'remind_at', 'expires_at', 'package_note', 'food_class', 'opened',
                'deadline_source', 'status', 'storage_note'):
        if key not in supplied_fields:
            p[key] = old.get(key)
    for key in ('date_source', 'date_phrase', 'date_reference_at', 'date_timezone',
                'time_precision', 'date_warnings'):
        if key in old and p.get('started_at') == old.get('started_at'):
            p[key] = old[key]
    changed = any(p.get(k) != old.get(k) for k in ('storage', 'condition', 'temperature_c',
                                                   'location', 'food_class', 'opened'))
    # 客户端显式给了期限/来源（例如清空自设日期）时以客户端为准，不强行沿用旧依据
    explicit_basis = bool({'deadline_source', 'expires_at'} & set(supplied_fields))
    history = list(old.get('storage_history', []))
    if changed:
        history.append({**{k: old.get(k) for k in ('storage', 'condition', 'temperature_c', 'location',
                                                   'expires_at', 'started_at')},
                        'changed_at': now().isoformat()})
        # 变更不能直接套新期限；需重新核实，保留原始日期与依据，不自动续期。
        preserve = ['started_at', 'date_type', 'package_note',
                    'rule_id', 'rule_version', 'rule_source', 'rules_status']
        if not explicit_basis:
            preserve += ['expires_at', 'deadline_source']
        for k in preserve:
            if context_confirmed and k in ('started_at', 'date_type', 'package_note') and k in supplied_fields:
                continue
            p[k] = old.get(k)
    else:
        # 未变更条件时，规则依据字段始终以服务器记录为准。
        for k in ('rule_id', 'rule_version', 'rule_source', 'rules_status'):
            if old.get(k) is not None:
                p[k] = old.get(k)
    if not changed and reconfirmed:
        # 用户重新核对当前条件后可确认新日期；原始登记起点仍保留。
        for k in ('started_at', 'date_type'):
            p[k] = old.get(k)
    p.update(version=uid(), storage_history=history,
             needs_review=changed or (old.get('needs_review', False) and not reconfirmed))
    record.payload = p
    auto = _auto_reevaluate(p, changed and not reconfirmed)
    p.update(auto['payload_update'])
    if p.get('deadline_source') == 'package' and not _current_package_basis(p):
        p['needs_review'] = True
    if proposed_plan is not None:
        from . import consumption_plan
        if proposed_plan.get('fact_fingerprint') != consumption_plan.fingerprint(p):
            raise AppError('ACTION_STALE', '食用安排与当前记录不一致，请重新核对。', 409)
        p['consumption_plan'] = proposed_plan
    if 'remind_at' in supplied_fields and p.get('remind_at') != old.get('remind_at'):
        validate_reminder(p)
    record.payload = p
    sync_reminders(db, record)
    if commit:
        db.commit()
    else:
        db.flush()          # 由调用方（确认操作）与终态同事务提交
    record._auto_reevaluated = auto['info']          # 供路由读取，不入库
    return record


def _auto_reevaluate(p: dict, triggered: bool) -> dict:
    """条件变化后默认自动重评（本批复审已同意）。

    - 有已发布规则匹配 → 更新系统期限并重排提醒；
    - 无匹配 → 清空对外有效期限、标待核对（旧提醒由 sync_reminders 撤销）；
    - **不覆盖用户自设日期**，不清零历史，不自动续期。
    """
    from . import food_advice
    if not triggered:
        return {'payload_update': {}, 'info': {'auto_reevaluated': False}}
    if p.get('deadline_source') == 'user':
        return {'payload_update': {'needs_review': False},
                'info': {'auto_reevaluated': False, 'reason': 'user_date_preserved'}}
    advice = food_advice.assess(p)
    basis = advice['basis']
    if basis.get('type') == 'rule' and advice.get('date'):
        return {'payload_update': {'expires_at': advice['date'], 'deadline_source': 'rule',
                                  'needs_review': False, 'rule_id': basis.get('rule_id'),
                                  'rule_version': basis.get('rule_version'),
                                  'rule_source': basis.get('rule_source'),
                                  'rules_status': basis.get('note') and 'published'},
                'info': {'auto_reevaluated': True, 'reason': 'ok',
                         'rule_id': basis.get('rule_id')}}
    if basis.get('type') == 'package' and advice.get('date'):
        return {'payload_update': {'expires_at': advice['date'], 'deadline_source': 'package',
                                  'needs_review': False},
                'info': {'auto_reevaluated': True, 'reason': 'ok', 'basis': 'package'}}
    # 无适用依据：清空对外有效期限，保持待核对
    return {'payload_update': {'expires_at': None, 'deadline_source': 'unknown',
                              'needs_review': True, 'rule_id': None, 'rule_version': None,
                              'rule_source': None, 'rules_status': None},
            'info': {'auto_reevaluated': True, 'reason': advice['reason'],
                     'missing': advice['missing']}}


def remove(db, record: Record):
    core.advisory_lock(db, 'food', record.owner, record.id)
    db.refresh(record)
    cancel_reminders(db, record.id, record.owner)
    db.delete(record)
    db.commit()


# ---------- 期限重评 ----------
def _parse_time(value, label):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AppError('INVALID_TIME', f'{label}格式不正确，请提供带时区的日期时刻。', 422) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AppError('INVALID_TIME', f'{label}需要带时区。', 422)
    return parsed


def reevaluate(db, record: Record, data: ReevaluateInput, lock: bool = True) -> dict:
    """按新条件与证据重评：包装 → 已发布规则 → 用户自设 → 未知（待核对）。

    409 语义（不得改回统一回滚）：无适用/未发布规则时，**先提交**待核对状态、
    清空旧期限并取消旧提醒，然后才抛错。因此这些分支在服务内显式提交。
    """
    if lock:
        core.advisory_lock(db, 'food', record.owner, record.id)
        db.refresh(record)
    old = record.payload
    p = dict(old)
    supplied = data.model_fields_set
    changed_fields = {}
    cleared_fields = []
    for key in ('storage', 'condition', 'temperature_c', 'location', 'food_class', 'opened'):
        value = getattr(data, key, None)
        if key in supplied and value is None:
            if key in ('storage', 'condition'):
                if old.get(key) != 'unknown':
                    changed_fields[key] = 'unknown'
                continue
            if old.get(key) is not None:
                cleared_fields.append(key)
            continue
        if value is not None and value != old.get(key):
            changed_fields[key] = value
    for field in data.clear or []:
        if field == 'name':
            raise AppError('FIELD_REQUIRED', f'{field} 是必要字段，不能清空。', 422)
        if field not in ('temperature_c', 'remind_at', 'expires_at', 'package_note', 'location',
                         'storage', 'condition', 'started_at', 'date_type'):
            raise AppError('INVALID_FIELD', f'不支持清空字段：{field}', 422)
        if field in ('storage', 'condition', 'date_type'):
            if old.get(field) != 'unknown':
                changed_fields[field] = 'unknown'
        elif field == 'started_at':
            if old.get(field) is not None:
                cleared_fields.append(field)
        elif field == 'remind_at':
            p.pop('remind_at', None)
        elif field == 'expires_at':
            p['expires_at'] = None
        elif field == 'package_note':
            p['package_note'] = ''
        elif field == 'temperature_c' and old.get('temperature_c') is not None:
            if 'temperature_c' not in cleared_fields:
                cleared_fields.append('temperature_c')
        elif field == 'location':
            p['location'] = 'unknown'
    if changed_fields or cleared_fields:
        history = list(old.get('storage_history', []))
        history.append({**{k: old.get(k) for k in ('storage', 'condition', 'temperature_c', 'location',
                                                   'expires_at', 'started_at')},
                        'changed_at': now().isoformat(),
                        **({'cleared': cleared_fields} if cleared_fields else {})})
        p['storage_history'] = history
        for key in cleared_fields:
            p[key] = None
        p.update(changed_fields)

    ev = data.evidence
    expires = _parse_time(ev.expires_at, '所设日期')
    remind = _parse_time(ev.remind_at, '提醒时刻')
    basis = {'type': 'none', 'note': ''}

    if ev.deadline_source == 'package':
        if not expires or not (ev.package_note or '').strip():
            raise AppError('NEEDS_INFO', '包装依据需要同时提供说明与日期。', 422)
        # 与 /foods/assess 同一口径：说明里的储存/温度/开封条件必须与当前记录匹配
        from . import food_advice
        checks = {**p, 'expires_at': expires.isoformat(), 'package_note': ev.package_note.strip(),
                  'deadline_source': 'package'}
        ok, why = food_advice.package_applicable(checks)
        if not ok:
            p.update(needs_review=True, expires_at=None, deadline_source='unknown', version=uid())
            record.payload = p
            sync_reminders(db, record)
            db.commit()
            raise AppError('PACKAGE_NOT_APPLICABLE', f'包装依据不适用于当前条件：{why}', 409)
        p.update(expires_at=expires.isoformat(), deadline_source='package',
                 package_note=ev.package_note.strip(), needs_review=False,
                 rule_id=None, rule_version=None, rule_source=None, rules_status=None)
        basis = {'type': 'package', 'note': ev.package_note.strip()}
    elif ev.deadline_source == 'rule':
        # 发布门槛：正式期限与自动提醒只能来自“已发布且条件适用”的规则。
        rule, computed, reasons = storage_rules.estimate_deadline(p, ev.rule_id, published_only=True)
        if not rule:
            draft_rule, _, draft_reasons = storage_rules.estimate_deadline(p, ev.rule_id, published_only=False)
            # 未发布规则只用于审核说明：不生成正式期限，也不安排正式提醒。
            p.update(needs_review=True, expires_at=None, deadline_source='unknown',
                     rule_id=None, rule_version=None, rule_source=None,
                     rules_status=storage_rules.RULES_STATUS, version=uid())
            record.payload = p
            sync_reminders(db, record)
            db.commit()  # 先落库待核对状态，再返回 409（既有语义，保持不变）
            if draft_rule and not draft_reasons:
                raise AppError('RULE_NOT_PUBLISHED',
                               '当前匹配的储存规则尚未发布，只能作为参考说明，不能生成正式期限；'
                               '请改用手动日期或等待规则审核发布。', 409)
            detail = f"（缺少或不符：{'、'.join(reasons)}）" if reasons else ''
            raise AppError('NO_APPLICABLE_RULE',
                           f'没有适用于当前条件的储存规则{detail}，已标为待核对；请补充条件或改用手动日期。',
                           409)
        p.update(expires_at=computed.isoformat(), deadline_source='rule',
                 needs_review=False, rule_id=rule['rule_id'], rule_version=rule['version'],
                 rule_source=rule['source'], rules_status=rule['status'])
        basis = {'type': 'rule', 'rule_id': rule['rule_id'], 'rule_version': rule['version'],
                 'rule_source': rule['source'], 'rules_status': rule['status'], 'note': rule['note'],
                 'date_origin': rule['date_origin']}
    elif ev.deadline_source == 'user':
        if not expires:
            raise AppError('NEEDS_INFO', '请提供你设置的日期。', 422)
        p.update(expires_at=expires.isoformat(), deadline_source='user', needs_review=False,
                 rule_id=None, rule_version=None, rule_source=None, rules_status=None)
        basis = {'type': 'user', 'note': '用户设置，仅表示距该日期的时间，不是系统估算。'}
    else:
        p.update(expires_at=None, deadline_source='unknown',
                 needs_review=bool(changed_fields or cleared_fields),
                 rule_id=None, rule_version=None, rule_source=None, rules_status=None)

    if remind is not None:
        p['remind_at'] = remind.isoformat()
    if (p.get('remind_at') and p.get('expires_at')
            and datetime.fromisoformat(p['remind_at']) > datetime.fromisoformat(p['expires_at'])):
        raise AppError('INVALID_TIME', '提醒时刻不能晚于已设日期。', 422)
    p.update(version=uid())
    if remind is not None and p.get('remind_at') != old.get('remind_at'):
        validate_reminder(p)
    record.payload = p
    sync_reminders(db, record)
    db.commit()
    result = view(record)
    result['deadline_basis'] = {**result['deadline_basis'], **basis}
    return result
