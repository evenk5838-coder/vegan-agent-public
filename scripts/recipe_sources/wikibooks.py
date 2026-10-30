"""Conservative, offline extraction of cached Wikibooks Cookbook wikitext.

``parse(page)`` does no I/O and does not translate, convert quantities, infer a
diet, or approve a tutorial. Unfamiliar templates survive as literal source text
and produce quality flags. ``structural_complete`` describes extraction only;
``manual_reviewed`` and ``dietary_verified`` always remain False.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import re
from typing import Any
from urllib.parse import quote, urlencode


_HEADING = re.compile(r"^\s*(={1,6})\s*(.*?)\s*\1\s*$")
_LIST = re.compile(r"^\s*([*#;:]+)\s*(.*)$")
_NUMBER = re.compile(r"^[\d\s./⁄+\-−¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]+$")
_STEP_LABELS = {"procedure", "procedures", "preparation", "directions", "method", "methods", "instructions", "steps", "cooking method", "cooking instructions"}
_STOP_LABELS = {"notes", "tips", "warnings", "references", "see also", "external links", "equipment", "special equipment", "nutrition", "nutrition facts", "variations", "notes tips and variations", "notes tips and variation"}


@dataclass
class _Context:
    title: str
    flags: set[str] = field(default_factory=set)
    servings: list[str] = field(default_factory=list)
    yields: list[str] = field(default_factory=list)


def _normalized(value: str) -> str:
    return re.sub(r"[\s_]+", " ", value.strip()).casefold()


def _balanced_end(text: str, start: int, opening: str, closing: str) -> int | None:
    depth, pos = 1, start + len(opening)
    while pos < len(text):
        if text.startswith(opening, pos):
            depth += 1
            pos += len(opening)
        elif text.startswith(closing, pos):
            depth -= 1
            pos += len(closing)
            if depth == 0:
                return pos
        else:
            pos += 1
    return None


def _parameters(text: str) -> list[str]:
    """Split only top-level pipes, retaining pipes inside links/templates."""
    out: list[str] = []
    start = pos = braces = links = 0
    while pos < len(text):
        token = text[pos:pos + 2]
        if token == "{{":
            braces += 1
            pos += 2
        elif token == "}}" and braces:
            braces -= 1
            pos += 2
        elif token == "[[":
            links += 1
            pos += 2
        elif token == "]]" and links:
            links -= 1
            pos += 2
        else:
            if text[pos] == "|" and not braces and not links:
                out.append(text[start:pos])
                start = pos + 1
            pos += 1
    out.append(text[start:])
    return out


def _template(raw: str, ctx: _Context, depth: int) -> str:
    parts = _parameters(raw[2:-2])
    name = _normalized(parts[0]).removeprefix("template:")
    args = parts[1:]
    if name in {"recipe summary", "recipesummary"}:
        for argument in args:
            if "=" not in argument:
                continue
            key, value = argument.split("=", 1)
            key = _normalized(key)
            if key in {"servings", "serves", "yield"}:
                rendered = _plain(value, ctx, depth + 1).strip()
                if rendered:
                    (ctx.yields if key == "yield" else ctx.servings).append(rendered)
        return ""
    if name in {"recipe", "reflist", "references", "text default color", "div col", "div col end"} or name.startswith("defaultsort:"):
        return ""
    if name in {"incomplete recipe", "recipe stub", "incomplete", "stub"}:
        ctx.flags.add("source_incomplete")
        return ""
    if name in {"pagename", "fullpagename", "subpagename"} and not args:
        return ctx.title if name == "fullpagename" else ctx.title.rsplit("/", 1)[-1] if name == "subpagename" else re.sub(r"^Cookbook:", "", ctx.title, flags=re.I)
    if name == "!" and not args:
        return "|"
    rendered = [_plain(value, ctx, depth + 1).strip() for value in args]
    positional = [value for value in rendered if not re.match(r"^[\w -]+\s*=", value)]
    if name in {"frac", "sfrac"} and 1 <= len(positional) <= 3 and all(_NUMBER.fullmatch(value) for value in positional):
        if len(positional) == 1:
            return "1/" + positional[0]
        return positional[0] + "/" + positional[1] if len(positional) == 2 else positional[0] + " " + positional[1] + "/" + positional[2]
    if name in {"units", "unit"} and len(positional) == 2 and _NUMBER.fullmatch(positional[0]):
        return " ".join(positional)
    if name in {"g", "kg", "ml", "l"} and not args:
        return name
    if name == "cups" and len(positional) == 1 and _NUMBER.fullmatch(positional[0]):
        return positional[0] + " cups"
    if name in {"convert", "convert/3"} and len(positional) >= 2 and _NUMBER.fullmatch(positional[0]):
        # Keep ONLY explicit input quantities and their original unit. No conversion
        # value, target unit, precision or hidden template result is manufactured.
        quantity: list[str] = [positional[0]]
        index = 1
        operators = {"-", "–", "to", "and", "or", "x", "×", "by", "+/-", "±"}
        while index + 1 < len(positional) and positional[index] in operators and _NUMBER.fullmatch(positional[index + 1]):
            quantity.extend(positional[index:index + 2])
            index += 2
        if index < len(positional):
            ctx.flags.add("conversion_not_calculated")
            return " ".join(quantity + [positional[index]])
    if name in {"lang", "langx"} and len(positional) == 2:
        return positional[1]
    if name in {"cb", "w", "wp", "wikipedia"} and 1 <= len(positional) <= 2:
        return positional[-1].replace("_", " ")
    ctx.flags.add("unparsed_template:" + name[:120])
    # Preserve unfamiliar semantics instead of guessing which argument is content.
    return raw


def _templates(text: str, ctx: _Context, depth: int = 0) -> str:
    if depth > 20:
        ctx.flags.add("template_depth_limit")
        return text
    result: list[str] = []
    cursor = 0
    while (start := text.find("{{", cursor)) >= 0:
        result.append(text[cursor:start])
        if text.startswith("{{{", start):
            end = text.find("}}}", start + 3)
            if end < 0:
                ctx.flags.add("unclosed_template")
                result.append(text[start:])
                return "".join(result)
            ctx.flags.add("unparsed_parameter")
            result.append(text[start:end + 3])
            cursor = end + 3
            continue
        end = _balanced_end(text, start, "{{", "}}")
        if end is None:
            ctx.flags.add("unclosed_template")
            result.append(text[start:])
            return "".join(result)
        result.append(_template(text[start:end], ctx, depth))
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


def _links(text: str, ctx: _Context) -> str:
    result: list[str] = []
    cursor = 0
    while (start := text.find("[[", cursor)) >= 0:
        result.append(text[cursor:start])
        end = _balanced_end(text, start, "[[", "]]")
        if end is None:
            ctx.flags.add("unclosed_wikilink")
            result.append(text[start:])
            return "".join(result)
        inner = text[start + 2:end - 2]
        parts = inner.split("|")
        target = parts[0].strip()
        if not re.match(r"^(?:category|image|file):", target, re.I):
            display = parts[-1].strip() if len(parts) > 1 else re.sub(r"^Cookbook:\s*", "", target, flags=re.I)
            result.append(display.replace("_", " "))
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


def _plain(text: str, ctx: _Context, depth: int = 0) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref\b[^>]*>.*?</ref\s*>|<ref\b[^>]*/\s*>", "", text, flags=re.I | re.S)
    if re.search(r"<(?:script|style)\b", text, re.I):
        ctx.flags.add("unsafe_html_removed")
        text = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", text, flags=re.I | re.S)
    text = _links(_templates(text, ctx, depth), ctx)
    text = re.sub(r"\[(?:https?|ftp)://[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[((?:https?|ftp)://[^\]]+)\]", r"\1", text)
    text = re.sub(r"<br\s*/?>|</(?:p|div|li|tr)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"'{2,5}", "", text)
    text = re.sub(r"__(?:NO|FORCE|TOC|INDEX|NEWSECTION)[A-Z_]*__", "", text)
    return "\n".join(re.sub(r"[\t \u00a0]+", " ", html.unescape(line)).strip() for line in text.splitlines())


def _kind(label: str) -> str:
    value = re.sub(r"[^\w ]", "", _normalized(label))
    if value in {"ingredient", "ingredients", "ingredients and quantities", "ingredients and amounts"} or value.startswith("ingredients for "):
        return "ingredients"
    if value in _STEP_LABELS:
        return "steps"
    if value in {"servings", "serves", "yield", "yields"}:
        return "servings"
    if value in _STOP_LABELS or value.startswith(("notes ", "nutrition ")):
        return "stop"
    return ""


def _blocks(lines: list[str], ctx: _Context) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    in_table = False

    def flush() -> None:
        if current:
            value = "\n".join(current).strip()
            if value:
                blocks.append(value)
            current.clear()

    for line in lines:
        if line.startswith("{|"):
            flush()
            in_table = True
            ctx.flags.add("unparsed_table")
        if in_table:
            if line.startswith("|}"):
                in_table = False
            continue
        if not line.strip():
            flush()
            continue
        item = _LIST.match(line)
        if item:
            if len(item.group(1)) == 1:
                flush()
            current.append(item.group(2))
        else:
            current.append(line)
    flush()
    if in_table:
        ctx.flags.add("unclosed_table")
    return blocks


def parse(page: dict[str, Any]) -> dict[str, Any]:
    """Extract original-language recipe fields from one cached MediaWiki page.

    Categories are retained solely as source hints. Plain extraction does not
    check ingredients introduced in steps/notes, alternatives, diets, food
    safety, quantities, attribution rights or whether the recipe cooks well.
    """
    if not isinstance(page, dict):
        raise TypeError("Wikibooks parse() expects one cached page dict")
    source_title = page.get("title") if isinstance(page.get("title"), str) else ""
    title = re.sub(r"^Cookbook:\s*", "", source_title, flags=re.I).strip()
    ctx = _Context(source_title)
    revisions = page.get("revisions")
    revision = revisions[0] if isinstance(revisions, list) and revisions and isinstance(revisions[0], dict) else {}
    slots = revision.get("slots") if isinstance(revision.get("slots"), dict) else {}
    main = slots.get("main") if isinstance(slots.get("main"), dict) else {}
    raw = main.get("content", revision.get("*", ""))
    if not isinstance(raw, str):
        raw = ""
    if not raw.strip():
        ctx.flags.add("missing_content")
    if re.match(r"^\s*(?:<!--.*?-->\s*)*#redirect\b", raw, re.I | re.S):
        ctx.flags.add("redirect")
    if main.get("contentmodel") not in (None, "wikitext"):
        ctx.flags.add("unsupported_content_model")
    if page.get("missing"):
        ctx.flags.add("source_page_missing")
    if not title:
        ctx.flags.add("missing_title")
    revision_id = revision.get("revid")
    if not isinstance(revision_id, int) or isinstance(revision_id, bool) or revision_id <= 0:
        revision_id = None
        ctx.flags.add("missing_source_revision")
    categories = [row["title"] for row in page.get("categories", []) if isinstance(row, dict) and isinstance(row.get("title"), str)] if isinstance(page.get("categories", []), list) else []
    categories.extend("Category:" + match.strip() for match in re.findall(r"\[\[Category:([^\]|]+)(?:\|[^\]]*)?\]\]", raw, re.I))
    categories = list(dict.fromkeys(categories))
    if any("incomplete recipe" in category.casefold() for category in categories):
        ctx.flags.add("source_incomplete")
    cleaned = _plain(raw, ctx)
    stack: list[tuple[int, str, str]] = []
    sections: list[dict[str, Any]] = []
    review_sections: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    intro: list[str] = []
    seen_recipe_section = False
    for line in cleaned.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level, label = len(heading.group(1)), heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            own_kind = _kind(label)
            inherited = next((kind for _, _, kind in reversed(stack) if kind), "")
            kind = own_kind or inherited
            stack.append((level, label, own_kind))
            active = {"heading": label, "path": [name for _, name, _ in stack], "kind": kind, "lines": []}
            if kind in {"ingredients", "steps", "servings"}:
                seen_recipe_section = True
                sections.append(active)
            else:
                # Alternatives, warnings and notes may introduce ingredients.
                # Retain them for cross-section review without calling them steps.
                review_sections.append(active)
            continue
        if active is not None:
            active["lines"].append(line)
        if not seen_recipe_section:
            intro.append(line)
    for section in sections:
        section["text"] = "\n".join(section.pop("lines")).strip()
        section["items"] = _blocks(section["text"].splitlines(), ctx)
    for section in review_sections:
        section["text"] = "\n".join(section.pop("lines")).strip()
    ingredients = [item for section in sections if section["kind"] == "ingredients" for item in section["items"]]
    steps = [item for section in sections if section["kind"] == "steps" for item in section["items"]]
    serving_values = list(ctx.servings)
    serving_values.extend(section["text"] for section in sections if section["kind"] == "servings" and section["text"])
    if not serving_values:
        serving_values.extend(ctx.yields)
    if not serving_values:
        serving_values.extend(line.strip() for line in intro if re.search(r"\b(?:serves|servings?|makes|yield)\s*[:=]?\s*(?:\d|one\b|two\b|three\b|four\b|five\b|six\b|eight\b|ten\b|twelve\b)", line, re.I))
    servings = "\n".join(dict.fromkeys(serving_values)) or None
    paragraphs = re.split(r"\n\s*\n", "\n".join(intro))
    lead = next((paragraph.strip() for paragraph in paragraphs if paragraph.strip() and not paragraph.lstrip().startswith(("|", "{", "[", "#", "*")) and _normalized(paragraph) != _normalized(title)), "")
    body = re.sub(r"\s+", " ", lead).strip()[:600]
    if not ingredients:
        ctx.flags.add("missing_ingredients")
    if not steps:
        ctx.flags.add("missing_steps")
    if not servings:
        ctx.flags.add("missing_servings")
    blocking = {"redirect", "source_incomplete", "missing_content", "missing_title", "missing_ingredients", "missing_steps", "missing_source_revision", "unsupported_content_model", "source_page_missing", "unparsed_table", "unclosed_table", "unclosed_template", "unparsed_parameter", "template_depth_limit", "unclosed_wikilink"}
    complete = not bool(ctx.flags & blocking) and not any(flag.startswith("unparsed_template:") for flag in ctx.flags)
    source_url = "https://en.wikibooks.org/w/index.php?" + urlencode({"title": source_title, "oldid": revision_id}) if revision_id else "https://en.wikibooks.org/wiki/" + quote(source_title.replace(" ", "_"), safe=":/")
    return {
        "title": title, "ingredients": ingredients, "steps": steps, "servings": servings, "body": body,
        "source_url": source_url, "source_revision": revision_id, "source_title": source_title,
        "source_categories": categories, "source_category_hints_only": True,
        "source_license": page.get("license"), "source_retrieved_at": page.get("retrieved_at"),
        "sections": sections, "review_sections": review_sections,
        "quality_flags": sorted(ctx.flags), "structural_complete": complete,
        "manual_reviewed": False, "dietary_verified": False,
    }
