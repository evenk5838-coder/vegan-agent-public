"""知识条目导入链路 CLI：把人工整理并核对的条目导入为待审核状态。

用法：
    .venv/bin/python scripts/import_knowledge.py <file.json>

条目必须带 type/title/source；期限与规则类还必须带 conditions。
导入默认 status=draft（待审核），不会直接对外展示。
"""
import json
import hashlib
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'backend'))

from app import core, knowledge  # noqa: E402


def preview_batch(path, recompute=False):
    """批量预览：结构校验 + 可选按当前规则重算三类标签 + 异常报告。不写库。"""
    import json as _json
    from app.recipes import diet_labels
    from app.rules import VERSION, evaluate
    data = _json.loads(Path(path).read_text(encoding='utf-8'))
    entries = data.get('entries') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise knowledge.AppError('INVALID_ENTRY', '导入文件需为条目数组或包含 entries 数组的对象。', 422)
    rows, rejected, incomplete, unknown_all = [], [], 0, {}
    for entry in entries:
        try:
            clean = knowledge.validate_entry(entry)
        except knowledge.AppError as e:
            rejected.append({'title': entry.get('title') if isinstance(entry, dict) else None,
                             'error': {'code': e.code, 'message': e.message}})
            continue
        complete = bool(clean.get('ingredients') and clean.get('steps') and clean.get('source'))
        if not complete:
            incomplete += 1
        row = {'title': clean['title'], 'type': clean['type'],
               'entry_status': 'draft', 'requested_status': clean['status'], 'tutorial_complete': complete,
               'meal': clean.get('meal'), 'cuisine': clean.get('cuisine')}
        if recompute:
            result = evaluate('、'.join([*(clean.get('ingredients') or []),
                                      *knowledge.original_recipe_ingredients(clean)]),
                              bool(clean.get('ingredients')), [], False)
            row['diet_labels'] = {k: v['status'] for k, v in diet_labels(result).items()}
            row['pungent'] = result['pungent']
            for item in result['items']:
                if item['group'] == 'unknown':
                    unknown_all[item['name']] = unknown_all.get(item['name'], 0) + 1
        rows.append(row)
    return {'dry_run': True, 'rules_version': VERSION if recompute else None,
            'input_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'total': len(rows) + len(rejected), 'importable': len(rows),
            'rejected': rejected, 'incomplete_or_summary': incomplete,
            'would_publish': sum(1 for r in rows if r['entry_status'] == 'published'),
            'unknown_ingredients': dict(sorted(unknown_all.items(), key=lambda kv: -kv[1])),
            'unknown_count': len(unknown_all), 'rows': rows}


def preview(path):
    """只校验不写库：逐条报告结构问题与教程完整性。"""
    import json as _json
    data = _json.loads(Path(path).read_text(encoding='utf-8'))
    entries = data.get('entries') if isinstance(data, dict) else data
    rows = []
    for entry in entries:
        try:
            clean = knowledge.validate_entry(entry)
        except knowledge.AppError as e:  # type: ignore[attr-defined]
            rows.append({'title': entry.get('title') if isinstance(entry, dict) else None, 'status': 'rejected',
                         'error': {'code': e.code, 'message': e.message}})
            continue
        complete = bool(clean.get('ingredients') and clean.get('steps') and clean.get('source'))
        rows.append({'title': clean['title'], 'type': clean['type'],
                     'entry_status': 'draft', 'requested_status': clean['status'],
                     'tutorial_complete': complete,
                     'servings': clean.get('servings'), 'meal': clean.get('meal'),
                     'cuisine': clean.get('cuisine'),
                     'ingredient_count': len(clean.get('ingredients') or []),
                     'step_count': len(clean.get('steps') or [])})
    return {'dry_run': True, 'total': len(rows),
            'would_publish': sum(1 for r in rows if r.get('entry_status') == 'published'),
            'rows': rows}


def main(argv):
    args = [a for a in argv[1:] if not a.startswith('-')]
    flags = {a for a in argv[1:] if a.startswith('-')}
    if len(args) != 1:
        print('用法：python scripts/import_knowledge.py <file.json> [--dry-run] [--recompute]')
        return 2
    if '--dry-run' in flags:
        report = preview_batch(args[0], recompute='--recompute' in flags)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print('这是预览：未写入任何数据库。', file=sys.stderr)
        return 0
    core.init_db()
    report = knowledge.import_file(args[0])
    counts = report.get('counts') or {}
    rejected = [r for r in report['results'] if r['status'] == 'rejected']
    print(json.dumps({'counts': counts, 'total': report.get('total'),
                      'rejected_sample': rejected[:5]}, ensure_ascii=False, indent=2))
    print('提示：导入条目为 draft，需人工核对后发布；发布前不会对外展示。', file=sys.stderr)
    return 0 if not rejected else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
