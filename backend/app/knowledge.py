"""知识条目：导入、审核状态与查询链路。

四类知识：配料规则、储存期限、菜谱、营养。
- 条目统一存 records（kind='knowledge'），owner 恒为 'system'（公共内容，不属于任何账号）。
- 状态：draft（导入待审）→ reviewed（已核对）→ published（已发布，可对外展示）；archive 为下架。
- 未发布内容不在对外查询中返回；空库明确返回 empty，不伪造教程、营养结论或保质期。
- 每条必须带来源与适用条件，缺失时导入即拒绝。
"""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from sqlalchemy import func, select

from . import core
from .core import AppError, Record, now, uid

TYPES = ('ingredient_rule', 'storage', 'recipe', 'nutrition')
STATUSES = ('draft', 'reviewed', 'published', 'archived')
PUBLIC_STATUS = 'published'


def chinese_recipe_display_ready(payload: dict) -> bool:
    """中文显示标记还要有实际中文标题、食材和步骤，不能只改语言元数据。"""
    if payload.get('type') != 'recipe':
        return True
    values = [payload.get('title')] + list(payload.get('ingredients') or []) + list(payload.get('steps') or [])
    values += list(payload.get('notes') or [])
    if payload.get('body'):
        values.append(payload['body'])
    return bool(payload.get('ingredients') and payload.get('steps')
                and all(isinstance(value, str) and re.search(r'[\u3400-\u9fff]', value) for value in values))


def original_recipe_ingredients(payload: dict) -> list[str]:
    """翻译只改变显示，来源原料仍参与规则，防止漏译洗掉禁忌证据。"""
    meta = {**(payload.get('conditions') or {}), **(payload.get('source_meta') or {})}
    original = meta.get('original_content') or {}
    value = original.get('ingredients') if isinstance(original, dict) else None
    if isinstance(value, str) and value.strip():
        return [value]
    return [line for line in value if isinstance(line, str) and line.strip()] if isinstance(value, list) else []


def validate_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise AppError('INVALID_ENTRY', '知识条目格式不正确。')
    kind = entry.get('type')
    if kind not in TYPES:
        raise AppError('INVALID_ENTRY', f'未知知识类型：{kind}；可选 {"/".join(TYPES)}。')
    title = str(entry.get('title') or '').strip()
    if not title:
        raise AppError('INVALID_ENTRY', '知识条目需要标题。')
    source = str(entry.get('source') or '').strip()
    if not source:
        raise AppError('INVALID_ENTRY', '知识条目必须带来源，不能留空。')
    conditions = entry.get('conditions') or {}
    if not isinstance(conditions, dict):
        raise AppError('INVALID_ENTRY', '适用条件需为对象，例如 {"storage":"refrigerated"}。')
    if kind in ('storage', 'ingredient_rule') and not conditions:
        raise AppError('INVALID_ENTRY', '期限/规则类条目必须写明适用条件，不能默认通用。')
    body = str(entry.get('body') or '')
    if kind == 'recipe':
        for field, value, maximum in (('title', title, 200), ('body', body, 8000)):
            if len(value) > maximum:
                raise AppError('INVALID_ENTRY', f'菜谱 {field} 超出 {maximum} 字符，不能截断后导入。', 422)
    # 来源元数据：schema 未显式建模的字段统一放进 source_meta，导入后仍可追溯
    meta_keys = ('source_id', 'source_revision', 'license', 'language',
                 'quality_flags', 'source_servings_text', 'source_categories_are_hints',
                 'source_structural_complete', 'manual_reviewed', 'parser_note',
                 'machine_diet_labels', 'source_author', 'source_checked_at',
                 'source_completeness', 'candidate_id', 'machine_rules_version', 'machine_rules_sha256',
                 'canonical_ingredients', 'content_sha256', 'input_sha256',
                 'corpus_sha256', 'import_batch_id', 'review_eligibility',
                 'ai_source_review', 'review_scope', 'reviewed_source_sha256')
    meta_keys += ('translation', 'display_language', 'original_content',
                  'translation_quality_flags', 'translation_reviewed',
                  'translation_source_sha256', 'editorial_review_notes',
                  'source_additional_sections')
    provided_meta = entry.get('source_meta') or {}
    if not isinstance(provided_meta, dict):
        raise AppError('INVALID_ENTRY', '来源元数据需为对象。', 422)
    combined_meta = {**conditions, **provided_meta}
    source_meta = {k: combined_meta.get(k) for k in meta_keys if combined_meta.get(k) is not None}
    for name in ('translation', 'original_content'):
        if name in source_meta and not isinstance(source_meta[name], dict):
            raise AppError('INVALID_ENTRY', f'{name}需为对象。', 422)
    for name in ('quality_flags', 'translation_quality_flags'):
        if name in source_meta and (not isinstance(source_meta[name], list)
                                  or any(not isinstance(value, str) for value in source_meta[name])):
            raise AppError('INVALID_ENTRY', f'{name}需为字符串数组。', 422)
    clean = {'type': kind, 'title': title[:200], 'source': source[:500],
             'conditions': conditions, 'source_meta': source_meta,
             'body': body[:8000],
             'reviewer_note': str(entry.get('reviewer_note') or '')[:1000],
             'status': entry.get('status') if entry.get('status') in STATUSES else 'draft'}
    if kind == 'recipe':
        clean.update(_recipe_fields(entry))
    return clean


def _recipe_fields(entry: dict) -> dict:
    """菜谱条目的结构化字段：分量、食材、步骤、餐次、菜系、营养说明。

    缺字段不拒绝导入（可以是来源摘要），但会反映到 tutorial_complete。
    """
    def lines(value) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        text = str(value or '').strip()
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    servings = entry.get('servings')
    try:
        servings = int(servings) if servings not in (None, '') else None
    except (TypeError, ValueError):
        raise AppError('INVALID_ENTRY', 'servings 需要是整数。', 422) from None
    if servings is not None and not (1 <= servings <= 50):
        raise AppError('INVALID_ENTRY', 'servings 需在 1–50 之间。', 422)
    meal = entry.get('meal')
    cuisine = entry.get('cuisine')
    if meal not in (None, '', 'breakfast', 'lunch', 'dinner', 'afternoon_tea', 'main'):
        raise AppError('INVALID_ENTRY', f'不支持的餐次：{meal}', 422)
    if cuisine not in (None, '', 'chinese', 'western', 'other'):
        raise AppError('INVALID_ENTRY', f'不支持的菜系：{cuisine}', 422)
    structured = {name: lines(entry.get(name)) for name in ('ingredients', 'steps', 'notes')}
    for field, maximum in (('ingredients', 60), ('steps', 100), ('notes', 40)):
        if len(structured[field]) > maximum:
            raise AppError('INVALID_ENTRY', f'菜谱 {field} 超出 {maximum} 条，不能截断后导入。', 422)
    text_fields = {name: str(entry.get(name) or '') for name in ('servings_text', 'nutrition_notes')}
    for field, maximum in (('servings_text', 800), ('nutrition_notes', 2000)):
        if len(text_fields[field]) > maximum:
            raise AppError('INVALID_ENTRY', f'菜谱 {field} 超出 {maximum} 字符，不能截断后导入。', 422)
    return {**structured, 'servings_text': text_fields['servings_text'],
            'servings': servings, 'meal': meal or None, 'cuisine': cuisine or None,
            'nutrition_notes': text_fields['nutrition_notes']}


def _source_key(payload: dict) -> str | None:
    """幂等键：source_id + revision（缺 revision 时退化为 source_id）。"""
    meta = payload.get('source_meta') or {}
    source_id = meta.get('source_id')
    if not source_id:
        return None
    revision = meta.get('source_revision')
    return f'{source_id}@{revision}' if revision is not None else str(source_id)


def is_review_protected(payload: dict) -> bool:
    """机器导入和对账不能改写已审核、发布、下架或留有人工审核标记的资料。"""
    return bool(payload.get('status') in ('published', 'reviewed', 'archived')
                or payload.get('manual_reviewed')
                or (payload.get('source_meta') or {}).get('manual_reviewed')
                or (payload.get('conditions') or {}).get('manual_reviewed'))


def draft_payload(clean: dict, prior: dict | None = None) -> dict:
    """导入只能形成草稿。来源声称的审核/发布状态不构成项目的发布批准。"""
    prior = prior or {}
    meta = {**(clean.get('source_meta') or {}), 'manual_reviewed': False}
    conditions = {**(clean.get('conditions') or {})}
    if 'manual_reviewed' in conditions:
        conditions['manual_reviewed'] = False
    audit = list(prior.get('import_history') or [])
    audit.append({'source_key': _source_key(clean), 'at': now().isoformat(),
                  'input_sha256': meta.get('input_sha256'),
                  'corpus_sha256': meta.get('corpus_sha256'),
                  'rules_version': meta.get('machine_rules_version')})
    return {**clean, 'status': 'draft', 'source_meta': meta, 'conditions': conditions,
            'entry_id': prior.get('entry_id') or uid(),
            'imported_at': prior.get('imported_at') or now().isoformat(),
            'updated_at': now().isoformat(), 'import_history': audit,
            'status_history': list(prior.get('status_history') or [])}


def import_entries(entries: list[dict], owner: str = 'system', batch_dedupe: bool = True) -> dict:
    """批量导入为 draft；返回逐条结果（added/updated/skipped/rejected）。

    - 幂等：`source_id + revision` 相同视为同一条；整批内也去重，重跑不重复写入。
    - 保护：已发布（published）或已人工核对（reviewed / manual_reviewed）的条目不覆盖。
    """
    results = []
    seen_in_batch: set[str] = set()
    with core.DB() as db:
        core.advisory_lock(db, 'knowledge-source-import')
        existing_records = list(db.scalars(select(Record).where(Record.owner == owner,
                                                               Record.kind == 'knowledge')))
        existing = {(r.payload.get('type'), r.payload.get('title')) for r in existing_records}
        # 重复来源出现时优先保护任何已审核版本，不能用另一份草稿覆盖它。
        existing_records.sort(key=lambda r: is_review_protected(r.payload))
        by_source = {_source_key(r.payload): r for r in existing_records if _source_key(r.payload)}
        by_source_id = {}
        for r in existing_records:
            sid = (r.payload.get('source_meta') or {}).get('source_id')
            if sid:
                by_source_id[str(sid)] = r
        for entry in entries:
            try:
                clean = validate_entry(entry)
            except AppError as e:
                results.append({'title': entry.get('title') if isinstance(entry, dict) else None,
                                'status': 'rejected', 'error': {'code': e.code, 'message': e.message}})
                continue
            key = _source_key(clean)
            if batch_dedupe and key and key in seen_in_batch:
                results.append({'title': clean['title'], 'status': 'skipped',
                                'reason': 'duplicate_in_batch', 'source_key': key})
                continue
            if key:
                seen_in_batch.add(key)
            prior = by_source.get(key) if key else None
            sid = (clean.get('source_meta') or {}).get('source_id')
            any_revision = by_source_id.get(str(sid)) if sid else None
            if prior is None or (any_revision is not None and is_review_protected(any_revision.payload)):
                prior = any_revision
            if prior is not None:
                reviewed = is_review_protected(prior.payload)
                if reviewed:
                    results.append({'title': clean['title'], 'status': 'skipped',
                                    'reason': 'already_reviewed_or_published',
                                    'entry_id': prior.payload.get('entry_id')})
                    continue
                prior.payload = draft_payload(clean, prior.payload)
                if key:
                    by_source[key] = prior
                sid = (clean.get('source_meta') or {}).get('source_id')
                if sid:
                    by_source_id[str(sid)] = prior
                results.append({'title': clean['title'], 'status': 'updated',
                                'entry_id': prior.payload['entry_id']})
                continue
            # 有来源身份时，同名不同来源是不同资料，不能按标题吞掉。
            if not key and (clean['type'], clean['title']) in existing:
                results.append({'title': clean['title'], 'status': 'skipped', 'reason': 'duplicate'})
                continue
            record = Record(owner=owner, kind='knowledge', payload=draft_payload(clean))
            db.add(record)
            db.flush()
            existing.add((clean['type'], clean['title']))
            if key:
                by_source[key] = record
            sid = (clean.get('source_meta') or {}).get('source_id')
            if sid:
                by_source_id[str(sid)] = record
            results.append({'title': clean['title'], 'status': 'added',
                            'entry_id': record.payload['entry_id']})
        db.commit()
    counts = {'added': 0, 'updated': 0, 'skipped': 0, 'rejected': 0}
    for row in results:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return {'results': results, 'counts': counts, 'total': len(results)}


def import_file(path: str) -> dict:
    file = Path(path)
    if not file.is_file():
        raise AppError('NOT_FOUND', f'找不到导入文件：{path}', 404)
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise AppError('INVALID_ENTRY', '导入文件不是有效 JSON。', 422) from None
    entries = data.get('entries') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise AppError('INVALID_ENTRY', '导入文件需为条目数组或 {"entries": [...]}。', 422)
    input_sha256 = hashlib.sha256(file.read_bytes()).hexdigest()
    rows = []
    for entry in entries:
        if isinstance(entry, dict):
            conditions = entry.get('conditions')
            if conditions is None or isinstance(conditions, dict):
                entry = {**entry, 'conditions': {**(conditions or {}), 'input_sha256': input_sha256}}
        rows.append(entry)
    return import_entries(rows)


def public_items(kind: str | None = None) -> dict:
    with core.DB() as db:
        base = (Record.owner == 'system', Record.kind == 'knowledge')
        query = select(Record).where(*base, Record.payload['status'].as_string() == PUBLIC_STATUS)
        if kind:
            query = query.where(Record.payload['type'].as_string() == kind)
        # Do not fetch thousands of full draft recipes just to produce an empty
        # public library. Status filtering and the pending count belong in SQL.
        published = list(db.scalars(query.order_by(Record.created, Record.id)))
        pending = db.scalar(select(func.count(Record.id)).where(
            *base, Record.payload['status'].as_string().in_(('draft', 'reviewed')),
            func.coalesce(Record.payload['source_meta']['review_eligibility'].as_string(), '') != 'quarantined')) or 0
    items = [{'entry_id': r.payload.get('entry_id'), 'type': r.payload.get('type'),
              'title': r.payload.get('title'), 'source': r.payload.get('source'),
              'conditions': r.payload.get('conditions'), 'body': r.payload.get('body'),
              'reviewer_note': r.payload.get('reviewer_note'),
              # 菜谱结构化字段：缺了这些，公共条目就只剩一段正文，教程会丢步骤
              'ingredients': r.payload.get('ingredients') or [],
              'steps': r.payload.get('steps') or [],
              'notes': r.payload.get('notes') or [],
              'servings_text': r.payload.get('servings_text') or '',
              'servings': r.payload.get('servings'),
              'meal': r.payload.get('meal'), 'cuisine': r.payload.get('cuisine'),
              'nutrition_notes': r.payload.get('nutrition_notes') or '',
              'source_meta': r.payload.get('source_meta') or {}} for r in published]
    if not items:
        return {'state': 'empty', 'items': [], 'pending_review': pending,
                'notice': ('暂无已发布的知识条目；待审核内容不会作为教程、营养结论或期限依据展示。'
                           if pending == 0 else f'暂无已发布条目，另有 {pending} 条待审核内容，不计入结果。')}
    return {'state': 'published', 'items': items, 'pending_review': pending,
            'notice': '仅展示已发布条目；每条均含来源与适用条件，请按条件使用。'}


def published_recipes() -> list[dict]:
    """已发布的公共菜谱条目，作为“来源摘要”参与检索，不冒充完整教程。"""
    return [item for item in public_items('recipe')['items']]


def recipe_view(item: dict, preferences: dict | None = None) -> dict:
    """把公共菜谱条目转成检索结果结构；明确标注 scope 与是否完整教程。"""
    from .rules import VERSION as RULE_VERSION
    from .rules import evaluate, normalize_ingredients

    conditions = item.get('conditions') or {}
    ingredients = list(item.get('ingredients') or [])
    steps = list(item.get('steps') or [])
    original_ingredients = original_recipe_ingredients(item)
    # 有结构化食材与步骤且带来源才算完整教程；仅正文摘要一律按摘要处理
    rule_ingredients = normalize_ingredients('、'.join([*ingredients, *original_ingredients]))
    meta = item.get('source_meta') or {}
    quality_flags = meta.get('quality_flags') or []
    has_tutorial = bool(ingredients and steps and item.get('source'))
    summary_only = not has_tutorial
    return {'id': item['entry_id'], 'entry_id': item['entry_id'],
            'scope': 'public', 'origin': 'public_knowledge',
            'title': item['title'], 'source': item['source'], 'source_url': item['source'],
            'body': item.get('body') or '', 'conditions': conditions,
            'meal': item.get('meal') or conditions.get('meal'),
            'cuisine': item.get('cuisine') or conditions.get('cuisine'),
            'tags': list(conditions.get('tags') or []),
            'servings': item.get('servings') or conditions.get('servings'),
            'ingredients': ingredients, 'steps': steps,
            'notes': item.get('notes') or [],
            'servings_text': item.get('servings_text') or '',
            'original_ingredients': original_ingredients,
            # 展示用 ingredients 保留用量；规则判定用剥离用量后的名称（避免“蒜 2 瓣”漏判五辛）
            'rule_ingredients': rule_ingredients,
            'rule_version': RULE_VERSION,
            'nutrition_notes': item.get('nutrition_notes') or '',
            'contains_pungents': conditions.get('contains_pungents'),
            'tutorial_complete': not summary_only,
            'tutorial_status': 'source_summary' if summary_only else 'complete',
            # 结构完整但存在缺量/解析告警时，前端应显示“待补充”而不是“已完整”
            'quality_flags': quality_flags,
            'source_complete_unreviewed': bool(has_tutorial and quality_flags),
            'manual_reviewed': bool(meta.get('manual_reviewed')),
            'language': meta.get('language'),
            'display_language': meta.get('display_language') or meta.get('language'),
            'translation': meta.get('translation'),
            'source_revision': meta.get('source_revision'),
            'license': meta.get('license'),
            'source_servings_text': meta.get('source_servings_text'),
            'notice': ('这是来源事实摘要，不是完整教程；分量与步骤请以原文为准，确认后才保存到个人菜谱。'
                       if summary_only else '公共条目；仍以来源原文为准。'),
            'reviewer_note': item.get('reviewer_note'),
            'evaluation': evaluate('、'.join([*ingredients, *original_ingredients]) if ingredients else (item.get('body') or ''),
                                   True, [], (preferences or {}).get('avoid_coriander', False))}


def set_status(entry_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise AppError('INVALID_STATUS', f'未知状态：{status}', 422)
    with core.DB() as db:
        core.advisory_lock(db, 'knowledge-source-import')
        for record in db.scalars(select(Record).where(Record.owner == 'system', Record.kind == 'knowledge')):
            if record.payload.get('entry_id') == entry_id:
                meta = record.payload.get('source_meta') or {}
                language = str(meta.get('display_language') or meta.get('language') or '').casefold()
                translation = meta.get('translation') or {}
                if status == 'published' and (
                        meta.get('review_eligibility') == 'quarantined'
                        or meta.get('translation_quality_flags')
                        or (translation and translation.get('status') != 'complete')
                        or (language and not language.startswith('zh'))
                        or (language.startswith('zh') and not chinese_recipe_display_ready(record.payload))):
                    raise AppError('CONTENT_NOT_READY', '条目仍被隔离或中文教程未就绪，不能发布。', 409)
                history = list(record.payload.get('status_history', []))
                history.append({'from': record.payload.get('status'), 'to': status, 'at': now().isoformat()})
                record.payload = {**record.payload, 'status': status, 'status_history': history}
                db.commit()
                return {'entry_id': entry_id, 'status': status}
    raise AppError('NOT_FOUND', '找不到这条知识条目。', 404)
