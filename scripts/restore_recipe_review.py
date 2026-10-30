#!/usr/bin/env python3
"""Restore an offline recipe review bundle without collecting or reassessing it.

Only the explicitly selected directory named ``built`` is written. Existing
different outputs and symlinks are refused. The sole database this script opens
is a newly created, temporary review-search index inside that build directory.
No application modules, .env, business database or network services are used.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import sqlite3
import sys
import tempfile
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILT = ROOT / "docs/知识库/大批量建库-2026-10-26/built"
DIETS = ("pure", "vegan", "lacto_ovo")
STATUSES = {"suitable", "uncertain", "not_suitable"}
RECIPE_COLUMNS = ["id", "title", "source", "provider", "language", "structural_complete", "review_excluded", "labels", "record"]
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 8 * 1024 * 1024


class RestoreError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def checked_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise RestoreError("built directory must not be a symlink")
    resolved = path.resolve()
    if resolved.name != "built":
        raise RestoreError("destination must be a directory named built")
    for protected in (ROOT / "backend", ROOT / "frontend", ROOT / "data", ROOT / ".git"):
        if resolved == protected or protected in resolved.parents:
            raise RestoreError("destination must not be inside application/data/git directories")
    if not resolved.is_dir():
        raise RestoreError("built directory must already exist and contain the packaged inputs")
    return resolved


def regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RestoreError(f"expected regular non-symlink file: {path.name}")


def verify_hash(path: Path, expected: str) -> str:
    regular_file(path)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected or ""):
        raise RestoreError(f"expected a 64-character SHA-256 for {path.name}")
    actual = sha256_file(path)
    if actual != expected.lower():
        raise RestoreError(f"SHA-256 mismatch: {path.name}")
    return actual


def expand_gzip(source: Path, target: Path) -> None:
    size = 0
    with gzip.open(source, "rb") as stream, target.open("xb") as output:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UNCOMPRESSED_BYTES:
                raise RestoreError(f"uncompressed size bound exceeded: {source.name}")
            output.write(chunk)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_json(text: str):
    def invalid_constant(value):
        raise RestoreError(f"non-finite JSON number: {value}")
    return json.loads(text, object_pairs_hook=strict_object, parse_constant=invalid_constant)


def string_list(value, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RestoreError(f"{field} must be a list of strings")


def validate_corpus(path: Path) -> list[dict]:
    regular_file(path)
    if path.stat().st_size > MAX_UNCOMPRESSED_BYTES:
        raise RestoreError("corpus exceeds the uncompressed size bound")
    records, source_ids = [], set()
    with path.open("rb") as stream:
        number = 0
        while raw := stream.readline(MAX_JSONL_LINE_BYTES + 1):
            number += 1
            if len(raw) > MAX_JSONL_LINE_BYTES:
                raise RestoreError(f"JSONL line {number} exceeds the size bound")
            if not raw.strip():
                raise RestoreError(f"blank JSONL record at line {number}")
            try:
                row = load_json(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, RestoreError) as exc:
                raise RestoreError(f"invalid corpus JSONL at line {number}: {exc}") from exc
            if not isinstance(row, dict):
                raise RestoreError(f"corpus line {number} must be an object")
            for field in ("source_id", "title", "source", "provider"):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip() or "\0" in value:
                    raise RestoreError(f"invalid {field} at corpus line {number}")
            identity = row["source_id"]
            if identity != identity.strip() or re.search(r"[\x00-\x1f]", identity) or len(identity) > 2048:
                raise RestoreError(f"invalid source_id at corpus line {number}")
            if identity in source_ids:
                raise RestoreError(f"duplicate source_id at corpus line {number}")
            source_ids.add(identity)
            source_url = urlparse(row["source"])
            if source_url.scheme not in {"http", "https"} or not source_url.netloc:
                raise RestoreError(f"non-web source URL at corpus line {number}")
            for field in ("ingredients", "steps"):
                string_list(row.get(field), field)
            for field in ("structural_complete", "review_candidate_eligible"):
                if not isinstance(row.get(field), bool):
                    raise RestoreError(f"{field} must be a stored boolean at corpus line {number}")
            if row.get("status") != "draft" or row.get("publication_approved", False) is not False or row.get("manual_reviewed", False) is not False:
                raise RestoreError(f"corpus must contain unapproved drafts at line {number}")
            assessment = row.get("diet_assessment")
            if not isinstance(assessment, dict):
                raise RestoreError(f"missing stored diet assessment at corpus line {number}")
            labels = assessment.get("labels")
            if not isinstance(labels, dict) or set(labels) != set(DIETS):
                raise RestoreError(f"missing independent stored diet labels at corpus line {number}")
            for diet in DIETS:
                if not isinstance(labels[diet], dict) or labels[diet].get("status") not in STATUSES:
                    raise RestoreError(f"invalid stored {diet} label at corpus line {number}")
            string_list(assessment.get("canonical_ingredients"), "canonical_ingredients")
            records.append(row)
    if not records:
        raise RestoreError("corpus must contain at least one record")
    return records


def validate_draft(path: Path, records: list[dict]) -> int:
    regular_file(path)
    data = load_json(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise RestoreError("draft JSON must contain an entries list")
    expected = {row["source_id"]: row for row in records if row["review_candidate_eligible"]}
    seen = set()
    for index, entry in enumerate(data["entries"], 1):
        if not isinstance(entry, dict) or entry.get("type") != "recipe" or entry.get("status") != "draft":
            raise RestoreError(f"import entry {index} is not a recipe draft")
        conditions = entry.get("conditions")
        if not isinstance(conditions, dict):
            raise RestoreError(f"import entry {index} has no source conditions")
        identity = conditions.get("source_id")
        if not isinstance(identity, str) or identity not in expected or identity in seen:
            raise RestoreError(f"unknown, excluded or duplicate import source_id at entry {index}")
        seen.add(identity)
        row = expected[identity]
        if entry.get("source") != row["source"] or conditions.get("source_revision") != row.get("source_revision"):
            raise RestoreError(f"import/corpus provenance mismatch at entry {index}")
        if conditions.get("machine_diet_labels") != row["diet_assessment"]["labels"]:
            raise RestoreError(f"import/corpus stored label mismatch at entry {index}")
        if conditions.get("manual_reviewed", False) is not False:
            raise RestoreError(f"import entry {index} must remain unreviewed")
    if seen != set(expected):
        raise RestoreError("draft source IDs do not exactly match the stored eligible corpus IDs")
    return len(seen)


def create_index(path: Path, records: list[dict]) -> None:
    if path.exists() or path.is_symlink():
        raise RestoreError("new temporary index path must not already exist")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE recipes(id TEXT PRIMARY KEY,title TEXT,source TEXT,provider TEXT,language TEXT,structural_complete INTEGER,review_excluded INTEGER,labels TEXT,record TEXT)")
        db.execute('CREATE VIRTUAL TABLE recipe_fts USING fts5(id UNINDEXED,title,ingredients,steps,canonical,tokenize="unicode61")')
        for row in records:
            assessment = row["diet_assessment"]
            db.execute("INSERT INTO recipes VALUES(?,?,?,?,?,?,?,?,?)", (
                row["source_id"], row["title"], row["source"], row["provider"], row.get("source_language"),
                int(row["structural_complete"]), int(not row["review_candidate_eligible"]),
                json.dumps(assessment["labels"], ensure_ascii=False), json.dumps(row, ensure_ascii=False)))
            db.execute("INSERT INTO recipe_fts VALUES(?,?,?,?,?)", (
                row["source_id"], row["title"], "\n".join(row["ingredients"]), "\n".join(row["steps"]),
                " ".join(assessment["canonical_ingredients"])))
        db.commit()
        columns = [row[1] for row in db.execute("PRAGMA table_info(recipes)")]
        if columns != RECIPE_COLUMNS or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RestoreError("generated review index integrity/schema check failed")
        if db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] != len(records) or db.execute("SELECT COUNT(*) FROM recipe_fts").fetchone()[0] != len(records):
            raise RestoreError("generated review index row count mismatch")
        expected_excluded = sum(not row["review_candidate_eligible"] for row in records)
        if db.execute("SELECT COUNT(*) FROM recipes WHERE review_excluded=1").fetchone()[0] != expected_excluded:
            raise RestoreError("generated review exclusion flags do not match stored records")


def create_html(stage: Path, built: Path, records: list[dict]) -> None:
    report_path = built / "build-report.json"
    regular_file(report_path)
    report_bytes = report_path.read_bytes()
    report = load_json(report_bytes.decode("utf-8"))
    if not isinstance(report, dict) or report.get("published") != 0 or report.get("source_recipes_after_exact_dedup") != len(records) or report.get("review_candidates_after_exclusions") != sum(row["review_candidate_eligible"] for row in records):
        raise RestoreError("build-report counts do not match the packaged corpus")
    (stage / "build-report.json").write_bytes(report_bytes)
    # The existing generator is read-only and receives only the staging folder.
    # It renders stored labels; it does not invoke the source/normalizer builder.
    function = runpy.run_path(str(ROOT / "scripts/create_recipe_review.py"))["build"]
    function.__globals__["BUILT"] = stage
    with contextlib.redirect_stdout(io.StringIO()):
        function()
    regular_file(stage / "review.html")


def restore(built: Path, corpus_hash: str | None = None, draft_hash: str | None = None, *, index_only: bool = False, html: bool = False) -> dict:
    built = checked_directory(built)
    if not index_only and (not corpus_hash or not draft_hash):
        raise RestoreError("both --corpus-gz-sha256 and --draft-gz-sha256 are required")
    if not index_only:
        verify_hash(built / "corpus.jsonl.gz", corpus_hash)
        verify_hash(built / "knowledge-import-draft.json.gz", draft_hash)
    with tempfile.TemporaryDirectory(prefix=".restore-review-", dir=built) as temporary:
        stage = Path(temporary)
        outputs = []
        if index_only:
            regular_file(built / "corpus.jsonl")
            (stage / "corpus.jsonl").write_bytes((built / "corpus.jsonl").read_bytes())
        else:
            expand_gzip(built / "corpus.jsonl.gz", stage / "corpus.jsonl")
            expand_gzip(built / "knowledge-import-draft.json.gz", stage / "knowledge-import-draft.json")
            outputs.extend(["corpus.jsonl", "knowledge-import-draft.json"])
        records = validate_corpus(stage / "corpus.jsonl")
        draft_count = validate_draft(stage / "knowledge-import-draft.json", records) if not index_only else None
        create_index(stage / "recipe-search.sqlite3", records)
        outputs.append("recipe-search.sqlite3")
        if html:
            create_html(stage, built, records)
            outputs.append("review.html")
        # Preflight every destination before installing any artifact. Hard links
        # prevent an intervening writer from being overwritten after preflight.
        unchanged = []
        for name in outputs:
            target = built / name
            if target.exists() or target.is_symlink():
                regular_file(target)
                if sha256_file(target) != sha256_file(stage / name):
                    raise RestoreError(f"different existing output preserved without overwrite: {name}")
                unchanged.append(name)
        installed = []
        for name in outputs:
            if name not in unchanged:
                os.link(stage / name, built / name)
                installed.append(name)
        return {"built": str(built), "corpus_records": len(records), "draft_entries": draft_count,
                "review_excluded": sum(not row["review_candidate_eligible"] for row in records),
                "installed": installed, "reused_identical": unchanged,
                "index_sha256": sha256_file(built / "recipe-search.sqlite3"),
                "labels_recomputed": False, "business_database_written": False, "published": 0}


def self_test() -> dict:
    import copy
    checks = []
    labels = {diet: {"status": "uncertain"} for diet in DIETS}
    rows = [{"source_id": f"sample:{n}", "title": title, "source": f"https://example.org/{n}",
             "provider": "sample", "source_language": "en", "ingredients": ["1 cup lentils"],
             "steps": ["Boil lentils."], "structural_complete": True, "review_candidate_eligible": eligible,
             "body": "Boil the sample lentils.", "servings": None, "all_diets_rejected": False,
             "quality_flags": [], "source_license": "sample-only",
             "status": "draft", "publication_approved": False, "manual_reviewed": False,
             "source_revision": "sample-revision", "diet_assessment": {"labels": copy.deepcopy(labels), "canonical_ingredients": ["lentils"]}}
            for n, title, eligible in [(1, "Lentil soup", True), (2, "Excluded source", False)]]
    def fixture(parent: Path, source_rows=None, draft_ids=None):
        built = parent / "built"
        built.mkdir(parents=True)
        source_rows = rows if source_rows is None else source_rows
        entries = [{"type": "recipe", "title": row["title"], "source": row["source"], "status": "draft",
                    "conditions": {"source_id": row["source_id"], "source_revision": row["source_revision"], "machine_diet_labels": row["diet_assessment"]["labels"], "manual_reviewed": False}}
                   for row in rows if row["review_candidate_eligible"]]
        if draft_ids:
            entries[0]["conditions"]["source_id"] = draft_ids
        data = "".join(json.dumps(row) + "\n" for row in source_rows).encode()
        with gzip.open(built / "corpus.jsonl.gz", "wb") as output:
            output.write(data)
        with gzip.open(built / "knowledge-import-draft.json.gz", "wb") as output:
            output.write(json.dumps({"entries": entries}).encode())
        return built, sha256_file(built / "corpus.jsonl.gz"), sha256_file(built / "knowledge-import-draft.json.gz")
    def rejected(call, name):
        try:
            call()
        except (RestoreError, OSError, json.JSONDecodeError):
            checks.append(name)
        else:
            raise AssertionError(f"negative check did not reject: {name}")
    with tempfile.TemporaryDirectory(prefix="recipe-restore-test-") as temporary:
        root = Path(temporary)
        built, ch, dh = fixture(root / "valid")
        result = restore(built, ch, dh)
        assert result["corpus_records"] == 2 and result["draft_entries"] == 1
        with sqlite3.connect(built / "recipe-search.sqlite3") as db:
            assert [r[1] for r in db.execute("PRAGMA table_info(recipes)")] == RECIPE_COLUMNS
            assert db.execute("SELECT review_excluded FROM recipes ORDER BY id").fetchall() == [(0,), (1,)]
            assert db.execute("SELECT COUNT(*) FROM recipe_fts WHERE recipe_fts MATCH 'lentils'").fetchone()[0] == 2
            assert json.loads(db.execute("SELECT labels FROM recipes WHERE id='sample:1'").fetchone()[0]) == labels
        checks.append("restore_schema_fts_stored_labels_and_review_excluded")
        assert restore(built, ch, dh)["installed"] == []
        checks.append("identical_outputs_are_reused")
        bad, ch, dh = fixture(root / "bad-hash")
        rejected(lambda: restore(bad, "0" * 64, dh), "compressed_hash_mismatch")
        assert not (bad / "corpus.jsonl").exists()
        bad, ch, dh = fixture(root / "duplicate", rows + [rows[0]])
        rejected(lambda: restore(bad, ch, dh), "duplicate_source_id")
        bad, ch, dh = fixture(root / "draft-id", draft_ids="unknown:999")
        rejected(lambda: restore(bad, ch, dh), "foreign_draft_source_id")
        bad, ch, dh = fixture(root / "existing")
        (bad / "corpus.jsonl").write_bytes(b"user data preserved")
        rejected(lambda: restore(bad, ch, dh), "different_existing_output")
        assert (bad / "corpus.jsonl").read_bytes() == b"user data preserved"
        assert not (bad / "recipe-search.sqlite3").exists()
        bad, ch, dh = fixture(root / "symlink")
        (bad / "recipe-search.sqlite3").symlink_to(root / "outside.sqlite3")
        rejected(lambda: restore(bad, ch, dh), "symlink_destination")
        rejected(lambda: checked_directory(root), "destination_must_be_built")
        rejected(lambda: load_json('{"source_id":"a","source_id":"b"}'), "duplicate_json_key")
        index, ch, dh = fixture(root / "index-mode")
        with gzip.open(index / "corpus.jsonl.gz", "rb") as stream:
            (index / "corpus.jsonl").write_bytes(stream.read())
        assert restore(index, index_only=True)["review_excluded"] == 1
        checks.append("index_only_preserves_stored_review_excluded")
        rendered, ch, dh = fixture(root / "html")
        (rendered / "build-report.json").write_text(json.dumps({
            "source_recipes_after_exact_dedup": 2, "review_candidates_after_exclusions": 1,
            "structurally_complete": 2, "published": 0}))
        assert "review.html" in restore(rendered, ch, dh, html=True)["installed"]
        assert 'type="application/json"' in (rendered / "review.html").read_text(encoding="utf-8")
        checks.append("optional_html_from_stored_report_and_labels")
    return {"checks_passed": len(checks), "checks": checks, "network_used": False, "business_database_written": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--built", type=Path, default=DEFAULT_BUILT)
    parser.add_argument("--corpus-gz-sha256", help="expected hash from the delivered bundle")
    parser.add_argument("--draft-gz-sha256", help="expected hash from the delivered bundle")
    parser.add_argument("--index-only", action="store_true", help="use existing corpus.jsonl; never reassess or import it")
    parser.add_argument("--html", action="store_true", help="also render HTML using the existing generator and bundled build-report.json")
    parser.add_argument("--self-test", action="store_true", help="use only temporary miniature fixtures")
    args = parser.parse_args()
    try:
        report = self_test() if args.self_test else restore(args.built, args.corpus_gz_sha256, args.draft_gz_sha256, index_only=args.index_only, html=args.html)
    except (RestoreError, OSError, gzip.BadGzipFile, EOFError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
