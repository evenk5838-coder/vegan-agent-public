#!/usr/bin/env python3
"""Collect only pinned HowToCook recipe Markdown and LICENSE.

Uses a previously captured source-audit.json and public raw GitHub URLs. No
application imports, environment files, databases, model APIs, or media downloads.
Every downloaded recipe is checked against the Git blob SHA from the audit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = ROOT / "docs/知识库/大批量建库-2026-10-26"
USER_AGENT = "VeganAgentRecipeArchive/0.1 (public open recipe text audit)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def fetch(url: str, attempts: int = 3) -> tuple[bytes | None, int, str | None]:
    """Bounded retries; public content only. Return errors without response bodies."""
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(f"unexpected HTTP {response.status}")
                data = response.read(1_048_577)
            if len(data) > 1_048_576:
                raise ValueError("text exceeds 1 MiB limit")
            data.decode("utf-8")
            return data, attempt, None
        except (urllib.error.URLError, OSError, UnicodeError, ValueError, RuntimeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 401, 403, 404, 410}:
                return None, attempt, error
            if attempt < attempts:
                time.sleep(attempt)
    return None, attempts, error


def safe_path(path: str) -> PurePosixPath:
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"unsafe source path: {path!r}")
    return value


def collect_one(item: dict, commit: str, output: Path) -> dict:
    path = str(safe_path(item["path"]))
    url = "https://raw.githubusercontent.com/Anduin2017/HowToCook/" + commit + "/" + urllib.parse.quote(path)
    destination = output / path
    result = {
        "path": path,
        "source_url": "https://github.com/Anduin2017/HowToCook/blob/" + commit + "/" + urllib.parse.quote(path),
        "raw_url": url,
        "commit_sha": commit,
        "expected_blob_sha1": item.get("sha"),
        "expected_bytes": item.get("size"),
        "license": "Unlicense",
    }
    if destination.exists():
        data = destination.read_bytes()
        if git_blob_sha1(data) == item.get("sha"):
            result.update(status="ok", cached=True, attempts=0, bytes=len(data), sha256=sha256(data), git_blob_sha1=git_blob_sha1(data))
        else:
            result.update(status="failed", cached=True, attempts=0, error="existing file hash mismatch; preserved without overwrite")
        return result
    data, attempts, error = fetch(url)
    if data is None:
        result.update(status="failed", cached=False, attempts=attempts, error=error)
        return result
    actual_blob = git_blob_sha1(data)
    if actual_blob != item.get("sha"):
        result.update(status="failed", cached=False, attempts=attempts, error="downloaded Git blob SHA does not match captured tree", actual_blob_sha1=actual_blob)
        return result
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".download.tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    result.update(status="ok", cached=False, attempts=attempts, bytes=len(data), sha256=sha256(data), git_blob_sha1=actual_blob)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_BASE / "source-audit.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_BASE / "raw/howtocook")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--limit", type=int, default=0, help="optional bounded smoke-test sample")
    args = parser.parse_args()
    audit_bytes = args.audit.read_bytes()
    audit = json.loads(audit_bytes)["howtocook"]
    commit = audit.get("commit_sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        parser.error("audit must contain an explicitly verified 40-character commit_sha")
    items = [item for item in audit["recipe_markdown_manifest"] if item["path"].startswith("dishes/") and item["path"].endswith(".md") and not item["path"].startswith("dishes/template/")]
    if len({item["path"] for item in items}) != len(items):
        parser.error("duplicate paths in audit manifest")
    items.sort(key=lambda item: item["path"])
    if args.limit:
        items = items[:args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    audit_snapshot_name = "source-audit-" + sha256(audit_bytes) + ".json"
    audit_snapshot = args.output / audit_snapshot_name
    if not audit_snapshot.exists():
        audit_snapshot.write_bytes(audit_bytes)
    started = dt.datetime.now(dt.timezone.utc)
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(collect_one, item, commit, args.output) for item in items]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            records.append(result)
            if len(records) % 25 == 0 or result["status"] != "ok" or len(records) == len(items):
                print(json.dumps({"completed": len(records), "total": len(items), "ok": sum(row["status"] == "ok" for row in records), "last_status": result["status"], "last_path": result["path"]}, ensure_ascii=False), flush=True)
    license_url = f"https://raw.githubusercontent.com/Anduin2017/HowToCook/{commit}/LICENSE"
    license_data, license_attempts, license_error = fetch(license_url)
    license_record = {"raw_url": license_url, "path": "LICENSE", "attempts": license_attempts}
    if license_data is None:
        license_record.update(status="failed", error=license_error)
    else:
        license_path = args.output / "LICENSE"
        if license_path.exists() and license_path.read_bytes() != license_data:
            license_record.update(status="failed", error="existing LICENSE differs; preserved without overwrite")
        else:
            license_path.write_bytes(license_data)
            license_record.update(status="ok", bytes=len(license_data), sha256=sha256(license_data), git_blob_sha1=git_blob_sha1(license_data))
    manifest = {
        "schema_version": 1,
        "source": "Anduin2017/HowToCook",
        "license": "Unlicense",
        "license_source": "https://github.com/Anduin2017/HowToCook/blob/" + commit + "/LICENSE",
        "commit_sha": commit,
        "started_at_utc": started.isoformat(),
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "audit_sha256": sha256(audit_bytes),
        "audit_snapshot": audit_snapshot_name,
        "workers": args.workers,
        "is_limited_sample": bool(args.limit),
        "expected_recipe_count": len(items),
        "success_count": sum(row["status"] == "ok" for row in records),
        "failure_count": sum(row["status"] != "ok" for row in records),
        "cached_count": sum(row.get("cached", False) and row["status"] == "ok" for row in records),
        "recipe_bytes": sum(row.get("bytes", 0) for row in records if row["status"] == "ok"),
        "scope": "raw text archive; not diet-approved, translated, cooked, imported or published",
        "license_file": license_record,
        "files": sorted(records, key=lambda row: row["path"]),
    }
    data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    version_name = "manifest-" + started.strftime("%Y%m%dT%H%M%S.%fZ") + ".json"
    (args.output / version_name).write_bytes(data)
    (args.output / "manifest.json").write_bytes(data)
    print(json.dumps({"manifest": str(args.output / version_name), "sha256": sha256(data), "success_count": manifest["success_count"], "failure_count": manifest["failure_count"], "license_status": license_record["status"]}, ensure_ascii=False), flush=True)
    return 0 if manifest["failure_count"] == 0 and license_record["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
