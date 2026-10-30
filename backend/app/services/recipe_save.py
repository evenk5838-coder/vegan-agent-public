"""个人菜谱写入：版本保护、创建幂等、候选确认与来源追溯。

服务只 flush，事务由调用方统一提交。所有新保护保持旧客户端兼容；
不做公共发布，不新增模型自主保存工具。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from ..core import AppError, Record, advisory_lock, digest, now, record_dict, uid

RECIPE_KIND = 'recipe'
CANDIDATE_KIND = 'candidate'
CREATE_RECEIPT_KIND = 'recipe_create'


def recipe_version(payload: dict) -> str:
    """Opaque version for new and legacy rows, without rewriting legacy data on GET."""
    if isinstance(payload.get('version'), str) and payload['version']:
        return payload['version']
    # id/created are envelope fields present in list DTOs, not editable recipe data.
    content = {k: v for k, v in payload.items() if k not in {'id', 'created', 'version'}}
    return digest(json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(',', ':')))


def view(record: Record) -> dict:
    return {**record_dict(record), 'version': recipe_version(record.payload)}


def load_personal(db, owner: str, identifier: str) -> Record:
    record = db.get(Record, identifier, populate_existing=True)
    if (not record or record.owner != owner or record.kind != RECIPE_KIND
            or (record.expires and record.expires <= now())):
        raise AppError('NOT_FOUND', '找不到这道个人菜谱。', 404)
    return record


def expected_version(header: str | None) -> str | None:
    """Accept one strong version token. Legacy clients may omit If-Match."""
    if header is None:
        return None
    token = header.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', token):
        raise AppError('INVALID_RECIPE_VERSION', '请重新读取菜谱版本后保存。', 422)
    return token


def create_personal(db, owner: str, payload: dict, idempotency_key: str | None = None) -> tuple[Record, bool]:
    """Create with an optional persistent receipt; recipe + receipt commit together.

    A replay never resurrects a deleted recipe. The same key with different user
    content is a conflict, even after deletion. No key preserves old POST behavior.
    """
    key_hash = None
    fingerprint = None
    if idempotency_key is not None:
        if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', idempotency_key):
            raise AppError('INVALID_IDEMPOTENCY_KEY', '保存标识格式不正确，请重新发起保存。', 422)
        key_hash = digest(idempotency_key)
        # Rule updates / account preference changes do not change the submitted recipe.
        content = {k: v for k, v in payload.items()
                   if k not in {'evaluation', 'review_status', 'version'}}
        fingerprint = digest(json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(',', ':')))
        advisory_lock(db, 'recipe_create', owner, key_hash)
        receipt = db.scalar(select(Record).where(
            Record.owner == owner, Record.kind == CREATE_RECEIPT_KIND,
            Record.payload['key_hash'].as_string() == key_hash))
        if receipt:
            if receipt.payload.get('fingerprint') != fingerprint:
                raise AppError('IDEMPOTENCY_CONFLICT', '这次保存标识已用于另一份菜谱内容，请先核对原保存结果。', 409)
            record = db.get(Record, receipt.payload.get('recipe_id'))
            if not record or record.owner != owner or record.kind != RECIPE_KIND:
                raise AppError('RECIPE_DELETED', '这次保存的菜谱已删除，不会自动重新创建。', 410)
            return record, False
    record = Record(owner=owner, kind=RECIPE_KIND, payload={**payload, 'version': uid()})
    db.add(record)
    db.flush()
    if key_hash:
        db.add(Record(owner=owner, kind=CREATE_RECEIPT_KIND,
                      payload={'key_hash': key_hash, 'fingerprint': fingerprint, 'recipe_id': record.id}))
    db.flush()
    return record, True


def update_personal(db, owner: str, identifier: str, payload: dict, expected: str | None) -> Record:
    """Lock, read, compare and update in one transaction; caller owns commit.

    If-Match clients get atomic conflict protection. Omitted headers retain legacy
    last-write-wins behavior, and must not be described as protected from stale edits.
    """
    advisory_lock(db, 'recipe', owner, identifier)
    record = load_personal(db, owner, identifier)
    if expected is not None and expected != recipe_version(record.payload):
        raise AppError('RECIPE_CHANGED', '菜谱已在其他位置修改，请重新读取、核对后再保存。', 409)
    record.payload = {**record.payload, **payload, 'version': uid()}
    db.flush()
    return record


def remove_personal(db, owner: str, identifier: str) -> None:
    """Use the same lock as PUT, so updates cannot recreate a concurrently deleted row."""
    advisory_lock(db, 'recipe', owner, identifier)
    db.delete(load_personal(db, owner, identifier))
    db.flush()


def find_by_candidate(db, owner: str, candidate_id: str) -> Record | None:
    """同一候选是否已经保存过（幂等判据）。"""
    for record in db.scalars(select(Record).where(Record.owner == owner, Record.kind == RECIPE_KIND)):
        if record.payload.get('candidate_id') == candidate_id:
            return record
    return None


def save_from_candidate(db, owner: str, payload: dict, candidate: Record) -> tuple[Record, bool]:
    """把候选保存为个人菜谱。返回 (记录, 是否本次新建)。

    并发安全：先按 owner + candidate_id 取**事务级 advisory lock**，再做去重检查与插入，
    锁一直持有到调用方提交/回滚。这样两个并发确认只会保存一条，重放继续返回同一 id。

    调用方负责事务提交与响应状态码；这里不 commit（不得破坏与候选状态的一致性）。
    """
    advisory_lock(db, 'candidate_save', owner, candidate.id)
    existing = find_by_candidate(db, owner, candidate.id)
    if existing is not None:
        return existing, False

    db.refresh(candidate)
    if candidate.payload.get('saved_recipe_id'):
        raise AppError('RECIPE_DELETED', '这项候选保存的个人菜谱已删除，不会自动重新创建。', 410)

    record = Record(owner=owner, kind=RECIPE_KIND,
                    payload={**payload,
                             'version': uid(),
                             'source_url': candidate.payload.get('source_url', ''),
                             'origin': 'web_confirmed_by_user',
                             'candidate_id': candidate.id})
    db.add(record)
    db.flush()  # 先落库拿到记录 id，再回写候选，避免引用空 id
    # 候选保留但标记已保存，便于追溯与重复确认时识别。
    candidate.payload = {**candidate.payload, 'status': '已保存到个人菜谱',
                         'saved_recipe_id': record.id, 'saved_at': now().isoformat()}
    return record, True
