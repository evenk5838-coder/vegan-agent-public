"""Conversation adapters for food facts; formal saves remain on confirmation cards."""
from __future__ import annotations

from .. import core, dates, drafts
from ..core import AppError
from . import food_drafts


def is_registration_reply(text: str) -> bool:
    """Only explicit, statement-only registration/correction can end at its card.

    Mixed tasks and questions still need the normal model/tool continuation.
    """
    if any(word in text for word in ('?', '？', '怎么', '如何', '多久', '几天', '能吃', '能放',
            '建议', '帮我查', '查询', '搜索', '餐厅', '饭店', '素食馆', '菜谱', '配料',
            '做法', '营养', '热量', '是否', '为什么', '提醒', '另外', '以及', '还要',
            '吗', '呢', '怎样', '多少', '哪天', '有没有', '能不能', '顺便', '再帮', '分析')):
        return False
    return any(word in text for word in ('记下来', '记下', '登记', '记录', '更新刚才', '改一下'))


def normalize_facts(raw: dict, reference_time: str | None = None, timezone: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise AppError('INVALID_ARGUMENTS', '请说明要补充的食材信息。', 422)
    allowed = {*drafts.ITEM_FIELDS, 'date_phrase', 'package_expiry_phrase'}
    if set(raw) - allowed:
        raise AppError('INVALID_ARGUMENTS', '包含不支持的食材信息，请重新核对。', 422)
    normalized = drafts.normalize_items([raw], reference_time, timezone)[0]
    normalized.update({key: value for key, value in raw.items()
                       if key in drafts.ITEM_FIELDS and value in (None, '')})
    expiry = raw.get('package_expiry_phrase')
    if expiry:
        parsed = dates.parse(str(expiry))
        if parsed['source'] != 'absolute':
            raise AppError('DATE_UNCLEAR', '请核对包装标注的完整到期年月日。', 422)
        if raw.get('expires_at') and raw['expires_at'] != parsed['started_at']:
            raise AppError('DATE_CONFLICT', '包装日期与填写日期不一致，请核对。', 422)
        if raw.get('deadline_source') not in (None, '', 'unknown', 'package'):
            raise AppError('DATE_CONFLICT', '包装日期不能同时作为其他期限来源。', 422)
        normalized.update(expires_at=parsed['started_at'], deadline_source='package',
                          package_note=(str(raw.get('package_note') or '').strip() +
                                        f'；包装标示到期日期：{expiry}（按日期记录，非实际时刻）').lstrip('；'))
    return normalized


def revise(owner: str, args: dict) -> dict:
    identifier, item_id = str(args.get('draft_id') or ''), str(args.get('item_id') or '')
    raw = args.get('changes')
    if not identifier or not item_id or not isinstance(raw, dict) or not raw:
        raise AppError('INVALID_ARGUMENTS', '请指明要补充的草稿和内容。', 422)
    with core.DB() as db:
        core.advisory_lock(db, 'draft', owner, identifier)
        record = food_drafts.load(db, identifier, owner)
        current = food_drafts.view(record)
        if not args.get('advice_digest') or args['advice_digest'] != current['advice_digest']:
            raise AppError('DRAFT_CHANGED', '草稿已变化，请重新读取后再补充。', 409)
        target = next((i for i in record.payload.get('items', []) if i.get('item_id') == item_id), None)
        if not target:
            raise AppError('NOT_FOUND', '找不到这条草稿项。', 404)
        facts = dict(raw)
        if facts.get('package_expiry_phrase') and 'package_note' not in facts:
            facts['package_note'] = target.get('package_note') or ''
        changes = normalize_facts(facts)
        # A new date statement replaces its previous date provenance as one unit.
        if 'date_phrase' in raw or 'started_at' in raw:
            for field in ('date_phrase', 'date_source', 'date_reference_at', 'date_timezone',
                          'time_precision', 'date_warnings', 'date_missing'):
                changes.setdefault(field, None)
        merged = {**target, **changes}
        drafts.to_food_payload(merged)  # reject invalid values before mutating the draft
        food_drafts.update_item(db, record, item_id, changes)
        return food_drafts.view(record)


def attach_guidance(owner: str, identifier: str, item_id: str) -> dict:
    """Explicitly requested eating plan, prepared before the existing save card."""
    from . import storage_guidance, consumption_plan
    with core.DB() as db:
        record = food_drafts.load(db, identifier, owner)
        item = next((i for i in record.payload['items'] if i['item_id'] == item_id), None)
        if not item or item.get('saved_food_id') or record.payload['status'] not in food_drafts.OPEN_STATUSES:
            raise AppError('DRAFT_CLOSED', '请在已保存食材中查看建议。', 409)
        facts = storage_guidance.GuidanceInput(**{k: item[k] for k in storage_guidance.FIELDS if k in item}).facts()
    # Missing basics are a clarification, not a reason to buy a search call.
    guidance = storage_guidance.clarification(facts) or storage_guidance.generate(owner, facts)
    with core.DB() as db:
        core.advisory_lock(db, 'draft', owner, identifier)
        record = food_drafts.load(db, identifier, owner)
        item = next((i for i in record.payload['items'] if i['item_id'] == item_id), None)
        if (not item or item.get('saved_food_id') or record.payload['status'] not in food_drafts.OPEN_STATUSES
                or consumption_plan.fingerprint(item) != consumption_plan.fingerprint(facts)):
            raise AppError('DRAFT_CHANGED', '食材信息已变化，请先查看当前草稿。', 409)
        # Only server-generated plan data can reach this path, never model dates.
        item['consumption_plan'] = guidance['consumption_plan']
        item['storage_guidance_summary'] = guidance.get('summary', '')
        record.payload = {**record.payload, 'updated': core.now().isoformat()}
        db.commit()
        return food_drafts.view(record)


def planning_reply(result: dict) -> dict | None:
    """Short receipts from actual tool results, without another model roundtrip."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    guidance = result.get('guidance') or result.get('preview_guidance') or {}
    item = next(iter(result.get('items') or []), {})
    plan = result.get('consumption_plan') or guidance.get('consumption_plan') or item.get('consumption_plan')
    if not plan:
        return None
    status = plan.get('status')
    if status == 'needs_context':
        questions = plan.get('questions') or guidance.get('conditions_to_check') or [plan.get('message', '请再说明购买时间和实际存放情况。')]
        text = '先确认一下：' + ''.join(questions[:3])
        return {'text': text, 'needs_input': True, 'input_prompt': ''.join(questions[:3]), 'citations': []}
    if status in ('ready', 'due'):
        target = datetime.fromisoformat(plan['target_at']).astimezone(ZoneInfo('Asia/Shanghai'))
        from ..core import now
        due = target <= now()
        text = ('已到建议食用时间，请先核对当前情况。' if due else f'建议优先安排在{target.month}月{target.day}日{target.hour:02d}:{target.minute:02d}前食用。')
        if plan.get('basis') == 'conservative_schedule':
            if not due:
                text = f'建议优先安排下一餐，食用安排目标不晚于{target.month}月{target.day}日{target.hour:02d}:{target.minute:02d}。'
            text += '这是保守安排，资料没有给出保存天数。'
        reminders = plan.get('key_reminders') or ['冷藏中断或出现异常时先核对；倒计时不保证食用安全。']
        text += '\n关键提醒：' + '；'.join(s.rstrip('。；') for s in reminders[:2]) + '。'
        if result.get('pending_action'):
            text += '\n请先确认更新记录，确认后冰箱才会采用这份安排。'
        elif result.get('draft_id'):
            text += '\n确认保存后，这份安排会一起放进冰箱。'
        return {'text': text, 'needs_input': False, 'input_prompt': '', 'citations': []}
    return {'text': '这次还没有找到适用的食用时间，请先核对实际存放情况，暂不生成具体食用日期。',
            'needs_input': False, 'input_prompt': '', 'citations': []}


def draft_context(db, owner: str, cards: list[dict]) -> list[dict]:
    """Refresh only drafts actually referenced by this session, never all users' data."""
    identifiers = []
    for card in reversed(cards):
        identifier = (card.get('payload') or {}).get('draft_id') if card.get('type') == 'food_draft' else None
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
        if len(identifiers) == 2:
            break
    result = []
    for identifier in reversed(identifiers):
        try:
            record = food_drafts.load(db, identifier, owner)
        except AppError as error:
            if error.code == 'NOT_FOUND':
                continue
            raise
        view = food_drafts.view(record)
        result.append({'draft_id': identifier, 'status': view['status'], 'advice_digest': view['advice_digest'],
                       'items': [{key: item[key] for key in (*drafts.ITEM_FIELDS, 'item_id', 'saved_food_id', 'date_phrase')
                                  if key in item} for item in view['items']]})
    return result
