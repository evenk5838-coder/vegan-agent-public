#!/usr/bin/env python3
"""Archive explicitly selected official historical cookbook texts.

Only Project Gutenberg catalog pages and their text/HTML links are read. Images,
whole-library dumps, application data and model services are outside this script.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/知识库/大批量建库-2026-10-26/raw/gutenberg"
BOOKS = {
    13887: "Dr. Allinson's Cookery Book / The Allinson Vegetarian Cookery Book",
    27639: "New Vegetarian Dishes",
    14594: "Cassell's Vegetarian Cookery",
    26209: "The Golden Age Cook Book",
    12238: "Science in the Kitchen",
    55555: "The Golden Rule Cook Book: Six hundred recipes for meatless dishes",
}


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url):
    if urlparse(url).netloc != "www.gutenberg.org":
        raise ValueError("only the observed official www.gutenberg.org host is allowed")
    for attempt in range(1, 4):
        try:
            request = Request(url, headers={"User-Agent": "VeganAgentSourceArchive/0.1 (historical cookbook research)"})
            with urlopen(request, timeout=40) as response:
                data = response.read(4_194_305)
                metadata = {"final_url": response.url, "last_modified": response.headers.get("Last-Modified"), "content_type": response.headers.get("Content-Type")}
            if len(data) > 4_194_304:
                raise ValueError("individual text exceeds 4 MiB bound")
            data.decode("utf-8-sig")
            return data, metadata, attempt
        except (OSError, ValueError) as exc:
            if attempt == 3:
                raise
            time.sleep(attempt)


def preserve_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        old = path.read_bytes()
        archive = path.with_name(path.stem + ".previous-" + digest(old) + path.suffix)
        if not archive.exists():
            archive.write_bytes(old)
    path.write_bytes(data)


def book(bookid, title):
    catalog_url = f"https://www.gutenberg.org/ebooks/{bookid}"
    catalog, catalog_meta, catalog_attempts = fetch(catalog_url)
    catalog_text = catalog.decode("utf-8-sig")
    if "Public domain in the USA." not in catalog_text:
        raise ValueError(f"book {bookid}: no expected catalog US public-domain statement")
    parser = Links()
    parser.feed(catalog_text)
    plain = next(h for h in parser.hrefs if h == f"/ebooks/{bookid}.txt.utf-8")
    html = next(h for h in parser.hrefs if h.startswith(f"/cache/epub/{bookid}/") and h.endswith(".html"))
    folder = OUTPUT / str(bookid)
    preserve_write(folder / "catalog.html", catalog)
    files = [{"format": "catalog", "path": f"{bookid}/catalog.html", "url": catalog_url, "sha256": digest(catalog), "bytes": len(catalog), "attempts": catalog_attempts, **catalog_meta}]
    for fmt, href, name in [("plain_text", plain, "book.txt"), ("html", html, "book.html")]:
        url = urljoin(catalog_url, href)
        data, metadata, attempts = fetch(url)
        if b"PROJECT GUTENBERG" not in data.upper():
            raise ValueError(f"book {bookid}: unexpected text without original Gutenberg notice")
        preserve_write(folder / name, data)
        files.append({"format": fmt, "path": f"{bookid}/{name}", "url": url, "sha256": digest(data), "bytes": len(data), "attempts": attempts, **metadata})
    result = {"book_id": bookid, "title": title, "catalog_url": catalog_url, "catalog_copyright_statement": "Public domain in the USA.", "license_policy_url": "https://www.gutenberg.org/policy/license.html", "copyright_scope": "Catalog asserts US public-domain status; original embedded Project Gutenberg notices retained; no publication approval implied.", "language": "en", "status": "ok", "files": files}
    print(json.dumps({"book_id": bookid, "status": "ok", "files": len(files), "bytes": sum(f["bytes"] for f in files)}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books", nargs="+", type=int, choices=BOOKS, default=list(BOOKS))
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    previous_bytes = (OUTPUT / "manifest.json").read_bytes() if (OUTPUT / "manifest.json").exists() else None
    previous = json.loads(previous_bytes) if previous_bytes else {}
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(book, bookid, BOOKS[bookid]): bookid for bookid in args.books}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"book_id": futures[future], "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    merged = {row["book_id"]: row for row in previous.get("books", [])}
    merged.update({row["book_id"]: row for row in results})
    all_results = sorted(merged.values(), key=lambda x: x["book_id"])
    manifest = {"scope": "selected historical cookbook source archives only; local drafts; no recipe synthesis, dietary approval, app import or publication", "started_at_utc": started.isoformat(), "completed_at_utc": datetime.now(timezone.utc).isoformat(), "workers": 2, "requested_book_ids_this_run": args.books, "previous_manifest_sha256": digest(previous_bytes) if previous_bytes else None, "books": all_results, "success_count": sum(x["status"] == "ok" for x in all_results), "failure_count": sum(x["status"] != "ok" for x in all_results)}
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    name = "manifest-" + started.strftime("%Y%m%dT%H%M%S.%fZ") + ".json"
    (OUTPUT / name).write_bytes(raw)
    (OUTPUT / "manifest.json").write_bytes(raw)
    print(json.dumps({"manifest": name, "sha256": digest(raw), "success_count": manifest["success_count"], "failure_count": manifest["failure_count"]}, ensure_ascii=False), flush=True)
    return int(manifest["failure_count"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
