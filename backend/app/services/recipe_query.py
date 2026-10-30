"""菜谱库内只读查询：HTTP 路由与 Agent 工具共用的唯一实现。

边界（本批按工作单限制）：
- 只做**库内只读查询**：本人私有菜谱 + 已发布公共条目，按同一套条件过滤。
- 不联网、不写库、不建候选、不计用量（联网补位仍由 HTTP 路由自己负责）。
- 不依赖 main.py、路由或内部 HTTP；只依赖数据访问与会话上下文。
- 账号隔离：个人条目按 owner 过滤；公共条目来自已发布知识，只读。
"""
from __future__ import annotations

import re

from sqlalchemy import select

from .. import core, knowledge
from ..core import Record, record_dict
from ..recipes import apply_filters


def load_personal(owner: str) -> list[dict]:
    """读取本人全部菜谱（按创建时间倒序，与既有接口一致）。"""
    with core.DB() as db:
        rows = db.scalars(select(Record).where(Record.owner == owner, Record.kind == 'recipe')
                          .order_by(Record.created.desc()))
        return [record_dict(row) for row in rows]


def load_public(preferences: dict) -> list[dict]:
    """读取已发布公共菜谱条目，转成检索视图（只读）。"""
    return [knowledge.recipe_view(item, preferences) for item in knowledge.published_recipes()]


SEARCH_FIELDS = ('title', 'ingredients', 'steps', 'notes', 'body', 'rule_ingredients',
                 'nutrition_notes', 'source', 'source_url')
# These are public applicability facts, not importer IDs, hashes or machine
# review metadata. The same extraction serves recipes and other knowledge.
CONDITION_SEARCH_FIELDS = ('applicability', 'applicable_to', 'applies_to', 'food', 'food_names',
                          'food_class', 'storage', 'condition', 'date_type', 'temperature',
                          'temperature_c', 'temperature_min_c', 'temperature_max_c',
                          'meal', 'cuisine', 'tags', 'purpose', 'scope', 'usage_notes',
                          'notes', 'warnings')

# Small, explicit ingredient families for everyday queries. Do not use the
# character “豆” alone: potatoes and nutmeg are not legumes. These are retrieval
# aliases only; dietary suitability still comes from the independent rules.
QUERY_FAMILIES = {
    '豆类': ('黄豆', '大豆', '黑豆', '绿豆', '红豆', '赤豆', '芸豆', '鹰嘴豆', '扁豆',
             '豌豆', '毛豆', '蚕豆', '豆腐', '豆干', '豆皮', '腐竹', '豆浆'),
    '豆制品': ('豆腐', '豆干', '豆皮', '腐竹', '豆浆', '千张', '素鸡'),
}


def search_fragments(row: dict) -> list[str]:
    """Extract source-backed searchable facts without exposing internal metadata."""
    def values(value):
        if isinstance(value, (list, tuple)):
            for item in value:
                yield from values(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from values(item)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            yield str(value)

    parts = []
    for field in SEARCH_FIELDS:
        parts.extend(values(row.get(field)))
    conditions = row.get('conditions') or {}
    if isinstance(conditions, dict):
        for field in CONDITION_SEARCH_FIELDS:
            parts.extend(values(conditions.get(field)))
    return parts


def _haystack(row: dict) -> str:
    return ' '.join(search_fragments(row)).casefold()


def search_excerpt(row: dict, query: str, maximum: int = 300) -> str:
    """Return a bounded verbatim excerpt, including matched ingredients/steps.

    A recipe with an empty introduction still needs actual evidence for the
    model to cite. This is a search excerpt, never a replacement full tutorial.
    """
    tokens = [token for token in re.split(r'[\s,，、;；/]+', (query or '').casefold()) if token]
    fragments = list(dict.fromkeys(search_fragments(row)))
    ranked = sorted(enumerate(fragments), key=lambda pair:
                    (-sum(token in pair[1].casefold() for token in tokens), pair[0]))
    return '\n'.join(fragment for _, fragment in ranked)[:maximum]


def _match_query(rows: list[dict], query: str | None) -> list[dict]:
    """多关键词匹配：标题/原料/步骤/摘要等字段命中**任一**关键词即算相关。

    整句必须完整出现才算命中会漏掉大量真实内容，这里按词拆分（空格/中文标点）。
    多个关键词时优先返回命中数多的条目（稳定排序：命中数 → 原顺序）。
    """
    if not query:
        return rows
    tokens = [t for t in re.split(r'[\s,，、;；/]+', query.casefold()) if t]
    if not tokens:
        return rows
    scored = []
    for index, row in enumerate(rows):
        hay = _haystack(row)
        hits = sum(1 for token in tokens if token in hay or
                   any(alias in hay for alias in QUERY_FAMILIES.get(token, ())))
        if hits:
            scored.append((-hits, index, row))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in scored]


def query_recipes(owner: str, filters: dict, preferences: dict, query: str | None = None) -> dict:
    """库内查询入口。

    参数：
      owner        当前账号 id（个人条目按此隔离）
      filters      recipes.normalize_filters() 的结果
      preferences  账号偏好（影响个人忌口与适配复检）
      query        可选标题关键词；None 表示不按关键词过滤

    返回：{'personal': [...], 'public': [...], 'items': personal + public}
    顺序：个人在前（创建时间倒序），公共在后（已发布顺序）。
    """
    personal = apply_filters(_match_query(load_personal(owner), query), filters, preferences)
    public = apply_filters(_match_query(load_public(preferences), query), filters, preferences)
    return {'personal': personal, 'public': public, 'items': personal + public}
