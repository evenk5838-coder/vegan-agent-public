"""Conservative, offline extraction for four archived Gutenberg cookbooks.

``parse_book(html, book_id, metadata)`` performs no I/O and returns source prose,
not a verified modern recipe.  The supported editions and selection rules are:

* 13887 (Allinson): the 517 h4 headings in the main food chapters from SOUPS
  AND STEWS through MISCELLANEOUS, less the two general essays GREEN VEGETABLES
  (General Remarks) and THE ADVANTAGES OF WHOLEMEAL BREAD, plus the 7 h4
  SANDWICHES entries: 522 candidates.  Indexes, menus, INVALID COOKERY,
  advertisements and later menu recipes are excluded.
* 27639 (New Vegetarian Dishes): only numbered recipe h4 headings 1 through 221.
  Its explicitly marked ``ul.ingredients`` is kept as an ingredient list.
* 26209 (Golden Age): food-chapter h3 headings from Bread, Biscuit, and Rolls
  through Sundries, stopping before Miscellaneous Recipes and CONTENTS.  The
  seven non-food household/cosmetic recipes in Miscellaneous Recipes are out.
* 55555 (Golden Rule): h3 headings inside 16 named food chapters, excluding
  VEGETABLE STOCK, which is an introductory stock-keeping discussion.  The
  archived edition has 649 h3 headings in those chapters, hence 648 retained;
  its title's "six hundred" is not used as a target count.  SUGGESTIVE COMMENTS,
  MENUS, the index and Colophon are excluded.

These counts describe the archived inputs, not quotas enforced by the parser.
Unknown book IDs raise ValueError; no generic heading heuristic is used to
inflate the collection with theory, menus, remedies or household recipes.
Ingredients remain empty unless the HTML explicitly marks an ingredient list.
``steps`` contains original source paragraphs (including notes), not inferred
imperative steps.  Whitespace is normalized and printed page markers removed;
no wording, ingredient, quantity, replacement, serving count or action is added.
"""
from __future__ import annotations

from bisect import bisect_left
import hashlib
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import quote, urlsplit


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCKS = {"p", "ul", "ol", "table", "pre", "blockquote", "h4", "h5", "h6"}
_GOLDEN_RULE_CHAPTERS = {
    "SOUPS", "VEGETABLES", "VEGETABLE COMBINATIONS", "NUT DISHES",
    "RICE MACARONI ETC", "CROQUETTES", "TIMBALES AND PATTIES", "SAUCES",
    "EGGS", "CHEESE", "SALADS", "SAVOURIES", "SANDWICHES",
    "PASTRY PATTY CASES ETC", "A FEW HOT BREADS", "PLUM PUDDING AND MINCE PIE",
}


class _Node:
    __slots__ = ("tag", "attrs", "start", "end", "parent", "children")

    def __init__(self, tag: str, attrs: dict[str, str], start: int,
                 end: int = 0, parent: _Node | None = None):
        self.tag = tag
        self.attrs = attrs
        self.start = start
        self.end = end
        self.parent = parent
        self.children: list[Any] = []


class _Document(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_starts = [0]
        self.line_starts.extend(match.end() for match in re.finditer("\n", source))
        self.root = _Node("document", {}, 0, len(source))
        self.stack = [self.root]
        self.nodes: list[_Node] = []
        self.feed(source)
        self.close()
        for node in self.stack:
            node.end = len(source)

    def _offset(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        start = self._offset()
        node = _Node(tag, {key: value or "" for key, value in attrs}, start,
                     start + len(self.get_starttag_text()), self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        end = self.source.find(">", self._offset()) + 1
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                for node in self.stack[index:]:
                    node.end = end
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _classes(node: _Node) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _raw_text(node: _Node) -> str:
    if node.tag in {"script", "style", "header", "footer"} or "pagenum" in _classes(node):
        return ""
    if node.tag == "br":
        return "\n"
    value = "".join(child if isinstance(child, str) else _raw_text(child) for child in node.children)
    return value + ("\n" if node.tag in {"p", "li", "tr", "div"} else "")


def _text(node: _Node) -> str:
    return re.sub(r"\s+", " ", _raw_text(node)).strip()


def _label(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip().upper()


def _descendants(node: _Node):
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _descendants(child)


def _ancestors(node: _Node):
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def _usable_anchor(value: str) -> bool:
    return bool(value) and not value.lower().startswith(("page_", "pb", "pg-", "img_"))


def _anchor(heading: _Node, nodes: list[_Node], previous_heading_end: int) -> tuple[str | None, str]:
    for node in [heading, *_descendants(heading)]:
        value = node.attrs.get("id") or node.attrs.get("name") or ""
        if _usable_anchor(value):
            return value, "recipe_heading"
    for node in _ancestors(heading):
        if "section" in _classes(node) and _usable_anchor(node.attrs.get("id", "")):
            return node.attrs["id"], "recipe_section"
    # Allinson puts an empty named anchor immediately before each h4.
    for node in reversed(nodes):
        if node.start >= heading.start:
            continue
        if node.start < previous_heading_end:
            break
        value = node.attrs.get("id") or node.attrs.get("name") or ""
        if node.tag == "a" and _usable_anchor(value):
            return value, "preceding_source_anchor"
    return None, "missing"


def _region_blocks(nodes: list[_Node], positions: list[int], start: int, end: int) -> list[_Node]:
    result: list[_Node] = []
    for node in nodes[bisect_left(positions, start):bisect_left(positions, end)]:
        if node.tag not in _BLOCKS or node.end > end:
            continue
        if any(parent.tag in _BLOCKS and parent.start >= start for parent in _ancestors(node)):
            continue
        if _text(node):
            result.append(node)
    return result


def _ingredient_items(blocks: list[_Node]) -> list[str]:
    items: list[str] = []
    for block in blocks:
        lists = [block, *_descendants(block)]
        for node in lists:
            marked = "ingredients" in _classes(node) or node.attrs.get("id", "").lower() == "ingredients"
            if node.tag in {"ul", "ol"} and marked:
                items.extend(_text(child) for child in node.children
                             if isinstance(child, _Node) and child.tag == "li" and _text(child))
    return items


def _is_ingredient_block(node: _Node) -> bool:
    return node.tag in {"ul", "ol"} and (
        "ingredients" in _classes(node) or node.attrs.get("id", "").lower() == "ingredients"
    )


def parse_book(html: str, book_id: int | str, metadata: dict) -> list[dict]:
    """Return source-backed candidates for the explicitly supported editions.

    ``metadata.html_url`` supplies the fetched official HTML URL and
    ``metadata.file_sha256`` the original file-byte hash.  If omitted, the URL
    uses Gutenberg's supported-edition cache path and the hash is computed over
    the given string's UTF-8 encoding (flagged as such, since callers may have
    normalized file newlines while reading).
    """
    if not isinstance(html, str) or not isinstance(metadata, dict):
        raise TypeError("parse_book expects HTML text and a metadata dict")
    ident = str(book_id)
    if ident not in {"13887", "27639", "26209", "55555"}:
        raise ValueError(f"No source-specific recipe parser for Gutenberg book {ident}")
    html_url = metadata.get("html_url") or f"https://www.gutenberg.org/cache/epub/{ident}/pg{ident}-images.html"
    parsed_url = urlsplit(html_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"www.gutenberg.org", "gutenberg.org"}:
        raise ValueError("metadata.html_url must be an official HTTPS Project Gutenberg URL")
    html_url = html_url.split("#", 1)[0]
    revision = metadata.get("file_sha256")
    supplied_revision = isinstance(revision, str) and bool(re.fullmatch(r"[0-9a-fA-F]{64}", revision))
    if not supplied_revision:
        revision = hashlib.sha256(html.encode("utf-8")).hexdigest()

    document = _Document(html)
    nodes = document.nodes
    positions = [node.start for node in nodes]
    headings = [node for node in nodes if re.fullmatch("h[1-6]", node.tag)]
    chapter = ""
    chapter_anchor: str | None = None
    allinson_main = False
    golden_age_main = False
    selected: list[tuple[_Node, str, int, str | None]] = []
    previous_heading_end = 0
    for heading in headings:
        title = _text(heading)
        label = _label(title)
        if heading.tag == "h2":
            chapter = title
            chapter_anchor, _ = _anchor(heading, nodes, previous_heading_end)
            if chapter_anchor is None:
                chapter_anchor = next((node.attrs["id"] for node in _ancestors(heading)
                                       if "chapter" in _classes(node) and node.attrs.get("id")), None)
            if ident == "13887":
                if label == "SOUPS AND STEWS":
                    allinson_main = True
                elif label == "A WEEK S MENU":
                    allinson_main = False
            elif ident == "26209":
                if label == "BREAD BISCUIT AND ROLLS":
                    golden_age_main = True
                elif label in {"MISCELLANEOUS RECIPES", "CONTENTS"}:
                    golden_age_main = False
        if ident == "27639" and heading.tag == "h3":
            chapter = title
        include = False
        if ident == "13887":
            include = heading.tag == "h4" and (allinson_main or _label(chapter) == "SANDWICHES")
            if label in {"GREEN VEGETABLES GENERAL REMARKS", "THE ADVANTAGES OF WHOLEMEAL BREAD"}:
                include = False
        elif ident == "27639" and heading.tag == "h4":
            match = re.match(r"No\.\s*(\d+)\.", title)
            include = bool(match and 1 <= int(match.group(1)) <= 221)
        elif ident == "26209":
            include = heading.tag == "h3" and golden_age_main
        elif ident == "55555":
            include = heading.tag == "h3" and _label(chapter) in _GOLDEN_RULE_CHAPTERS and label != "VEGETABLE STOCK"
        if include:
            selected.append((heading, chapter, previous_heading_end, chapter_anchor))
        previous_heading_end = heading.end

    results: list[dict] = []
    heading_positions = [node.start for node in headings]
    for heading, chapter, preceding_bound, chapter_anchor in selected:
        level = int(heading.tag[1])
        end = len(html)
        for following in headings[bisect_left(heading_positions, heading.end):]:
            if int(following.tag[1]) <= level:
                end = following.start
                break
        blocks = _region_blocks(nodes, positions, heading.end, end)
        ingredients = _ingredient_items(blocks)
        steps = [_text(node) for node in blocks if not _is_ingredient_block(node)]
        body_parts: list[str] = []
        for node in blocks:
            if _is_ingredient_block(node):
                body_parts.append("\n".join(_text(child) for child in node.children
                                            if isinstance(child, _Node) and child.tag == "li" and _text(child)))
            else:
                body_parts.append(_text(node))
        body = "\n\n".join(part for part in body_parts if part)
        anchor, anchor_kind = _anchor(heading, nodes, preceding_bound)
        recipe_anchor_missing = anchor is None
        if recipe_anchor_missing and chapter_anchor:
            anchor, anchor_kind = chapter_anchor, "chapter_fallback"
        flags = [
            "historical_source_requires_modern_recipe_review",
            "historical_units_not_converted",
            "modern_food_safety_not_reviewed",
            "dietary_suitability_not_verified",
            "original_english_not_translated",
            "html_whitespace_normalized_and_printed_page_markers_removed",
            "source_paragraphs_not_verified_as_step_sequence",
            "ingredient_inventory_and_step_crosscheck_not_done",
            "compound_ingredients_and_cross_references_not_expanded",
        ]
        if not ingredients:
            flags.append("no_separately_marked_ingredient_list_in_source")
        if not steps:
            flags.append("no_body_paragraphs_detected")
        if not body:
            flags.append("empty_source_body_requires_review")
        if len(body) < 120:
            flags.append("short_source_entry_requires_context_review")
        if any(_text(node).lower().startswith("note.") for node in blocks):
            flags.append("source_paragraphs_include_recipe_notes")
        if any(node.tag == "a" and node.attrs.get("href", "").startswith("#")
               and not any("pagenum" in _classes(parent) for parent in _ancestors(node))
               for block in blocks for node in _descendants(block)):
            flags.append("source_contains_internal_cross_references")
        if recipe_anchor_missing:
            flags.append("recipe_anchor_not_found")
        if anchor_kind == "chapter_fallback":
            flags.append("source_uses_real_chapter_anchor_not_recipe_specific_anchor")
        if not supplied_revision:
            flags.append("revision_hash_computed_from_supplied_html_string_utf8")
        results.append({
            "title": _text(heading),
            "ingredients": ingredients,
            "steps": steps,
            "source": html_url + ("#" + quote(anchor, safe="-._~:") if anchor else ""),
            "source_revision": str(revision).lower(),
            "body": body,
            "chapter": chapter,
            "quality_flags": flags,
            "source_book_id": ident,
            "source_anchor": anchor,
            "source_anchor_kind": anchor_kind,
        })
    return results
