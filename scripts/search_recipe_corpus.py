"""Search/extract local review recipes without publishing or using any model."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'docs/知识库/大批量建库-2026-10-26/built/recipe-search.sqlite3'


def search(query: str, diet: str | None = None, limit: int = 10,
           include_rejected: bool = False, index: Path = INDEX):
    if diet not in (None, 'pure', 'vegan', 'lacto_ovo'):
        raise ValueError('diet must be pure/vegan/lacto_ovo')
    cleaned = re.sub(r'怎么做|如何做|做法|菜谱|食谱|推荐|帮我|给我|请', ' ', query)
    tokens = re.findall(r'[a-zA-Z0-9]+|[\u3400-\u9fff]+', cleaned)
    if not tokens:
        return []
    expression = ' OR '.join('"' + token.replace('"', '""') + '"' for token in tokens)
    sql = ('SELECT r.record, bm25(recipe_fts,0,6,3,1,3) AS score '
           'FROM recipe_fts JOIN recipes r ON recipe_fts.id=r.id '
           'WHERE recipe_fts MATCH ?')
    params = [expression]
    if not include_rejected:
        sql += ' AND r.review_excluded=0'
    if diet:
        sql += ' AND json_extract(r.labels, ?)=?'
        params.extend([f'$.{diet}.status', 'suitable'])
    sql += ' ORDER BY score LIMIT ?'
    params.append(max(1, min(limit, 100)))
    with sqlite3.connect(f'file:{index}?mode=ro', uri=True) as db:
        records = db.execute(sql, params).fetchall()
    return [json.loads(value[0]) for value in records]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('query')
    parser.add_argument('--diet', choices=['pure', 'vegan', 'lacto_ovo'])
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--full', action='store_true', help='extract full source recipe text and labels')
    parser.add_argument('--include-rejected', action='store_true', help='review-only: also show all-diet rejected sources')
    args = parser.parse_args()
    started = time.perf_counter()
    rows = search(args.query, args.diet, args.limit, args.include_rejected)
    output = rows if args.full else [
        {'source_id': row['source_id'], 'title': row['title'], 'source': row['source'],
         'structural_complete': row.get('structural_complete', False),
         'diet_labels': {k: v['status'] for k, v in row['diet_assessment']['labels'].items()}}
        for row in rows]
    print(json.dumps({'query': args.query, 'diet': args.diet, 'returned': len(rows),
                      'milliseconds': round((time.perf_counter() - started) * 1000, 2),
                      'scope': 'local draft review index, not published product results',
                      'items': output}, ensure_ascii=False, indent=2))
