"""Conservatively read HowToCook Markdown without completing a recipe for it.

``parse(text)`` performs no I/O.  List values contain source Markdown blocks,
including their original markers, indentation, quantities and optional wording.
The ingredient blocks include both the ingredient/tool and calculation sections;
they are not a normalized or verified ingredient inventory.  ``*_text`` and
``sections`` retain section context for downstream review.
"""
from __future__ import annotations

import re
from typing import Any


_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$")
_SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"^([ \t]*)(?:[-+*][ \t]+|\d+[.)][ \t]+|\d+、[ \t]*|[一二三四五六七八九十]+、[ \t]*)")
_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百半]+)"
_PEOPLE = rf"{_NUMBER}(?:\s*[-~～至到]\s*{_NUMBER})?\s*(?:个\s*)?人"
_SERVING_STATEMENT = re.compile(
    rf"每\s*(?:份|人)|{_PEOPLE}\s*(?:份|食用|享用|吃|的版本|的量|的用量|量)"
    rf"|(?:可供|供|适合|适用|足够)\s*{_PEOPLE}"
    rf"|(?:本菜谱|本配方|此配方|以下配方|按照|按)\s*(?:为|是)?\s*{_PEOPLE}"
    rf"|{_NUMBER}\s*份\s*(?:的版本|食用|用量|食材)"
)


def _kind(heading: str) -> str:
    label = re.sub(r"[\s*_`：:（）()\[\]【】]", "", heading)
    if label.startswith("必备原料") or label in {
        "材料", "食材", "原料", "配料", "原材料", "主食材", "辅食材",
        "配料表", "材料准备", "食材准备", "食材和工具", "原料和工具",
    }:
        return "ingredients"
    if label.startswith("计算") or label in {
        "用量", "食材用量", "原料用量", "配料用量", "份量计算", "配方用量",
    }:
        return "calculation"
    if label.startswith(("操作", "做法")) or label in {
        "步骤", "制作步骤", "烹饪步骤", "烹饪方法", "制作方法", "制作过程",
    }:
        return "steps"
    if label in {"份数", "份量", "食用人数", "适用人数", "食谱份量", "产量"}:
        return "servings"
    return "other"


def _headings(lines: list[str]) -> dict[int, tuple[int, str, int]]:
    """Map a heading's first line to (level, original label, source line count)."""
    found: dict[int, tuple[int, str, int]] = {}
    fence_char = ""
    fence_length = 0
    for index, line in enumerate(lines):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not fence_char:
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char, fence_length = "", 0
            continue
        if fence_char:
            continue
        atx = _ATX_HEADING.match(line)
        if atx:
            found[index] = (len(atx.group(1)), atx.group(2), 1)
            continue
        if index and _SETEXT_HEADING.match(line) and lines[index - 1].strip():
            # Do not read a list item followed by a horizontal rule as a title.
            previous = lines[index - 1]
            if not _LIST_ITEM.match(previous) and index - 1 not in found:
                found[index - 1] = (1 if line.lstrip().startswith("=") else 2, previous.strip(), 2)
    return found


def _blocks(body: str) -> list[str]:
    """Split outer list items while leaving nested lists and their numbers intact."""
    lines = body.splitlines()
    list_indents: list[int] = []
    fenced = False
    fence_char = ""
    for line in lines:
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not fenced:
                fenced, fence_char = True, marker
            elif marker == fence_char:
                fenced = False
            continue
        match = _LIST_ITEM.match(line)
        if not fenced and match:
            list_indents.append(len(match.group(1).expandtabs(4)))
    outer_indent = min(list_indents) if list_indents else None
    blocks: list[str] = []
    current: list[str] = []
    in_list_item = False
    fenced = False
    fence_char = ""

    def flush() -> None:
        nonlocal current
        value = "\n".join(current).strip("\n")
        if value.strip():
            blocks.append(value)
        current = []

    for line in lines:
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not fenced:
                fenced, fence_char = True, marker
            elif marker == fence_char:
                fenced = False
            current.append(line)
            continue
        match = None if fenced else _LIST_ITEM.match(line)
        is_outer_item = bool(
            match and len(match.group(1).expandtabs(4)) == outer_indent
        )
        if is_outer_item:
            flush()
            current.append(line)
            in_list_item = True
        elif not fenced and _ATX_HEADING.match(line):
            flush()
            blocks.append(line)
            in_list_item = False
        elif not line.strip() and not in_list_item and not fenced:
            flush()
        else:
            current.append(line)
    flush()
    return blocks


def parse(text: str) -> dict[str, Any]:
    """Extract source fields, without inventing amounts, servings or missing steps.

    ``completeness`` reports detected structure and explicit verification limits.
    A present ingredient section or nonempty step list does not mean a complete
    tutorial.  Ingredients introduced only in steps remain in ``steps_text`` and
    require cross-section review; no automatic dietary conclusion is made.
    """
    if not isinstance(text, str):
        raise TypeError("HowToCook parse() expects Markdown text as str")
    lines = text.splitlines()
    headings = _headings(lines)
    title = next((label for level, label, _ in headings.values() if level == 1), "")
    sections: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    start = 0

    def close(end: int) -> None:
        nonlocal active
        if active is not None:
            active["body"] = "\n".join(lines[start:end]).strip("\n")
            active["markdown"] = "\n".join(lines[active["line"] - 1:end]).strip("\n")
            sections.append(active)
            active = None

    for index, (level, label, heading_lines) in headings.items():
        kind = _kind(label)
        # A subsection belongs to its current parent: e.g. 操作 > 原料处理.
        if active is not None and level > active["level"]:
            continue
        close(index)
        if level == 1:
            continue
        active = {"heading": label, "kind": kind, "level": level, "line": index + 1}
        start = index + heading_lines
    close(len(lines))

    ingredient_sections = [section for section in sections if section["kind"] in {"ingredients", "calculation"}]
    step_sections = [section for section in sections if section["kind"] == "steps"]
    ingredients = [block for section in ingredient_sections for block in _blocks(section["body"])]
    steps = [block for section in step_sections for block in _blocks(section["body"])]
    serving_lines: list[str] = []
    # Restrict generic serving detection to the opening text and recipe fields.
    # Nutrition notes such as “一份上述早餐的预估营养” are not a serving count.
    first_section_line = sections[0]["line"] - 1 if sections else len(lines)
    serving_regions = ["\n".join(lines[:first_section_line])]
    serving_regions.extend(section["body"] for section in ingredient_sections)
    for section in sections:
        if section["kind"] == "servings":
            statement = section["markdown"]
            if section["body"].strip() and statement not in serving_lines:
                serving_lines.append(statement)
    for region in serving_regions:
        for line in region.splitlines():
            if _SERVING_STATEMENT.search(line) and line not in serving_lines:
                serving_lines.append(line)

    issues: list[str] = []
    if not title:
        issues.append("未识别到一级标题，未从路径或正文猜测菜名。")
    if not ingredient_sections:
        issues.append("未识别到原料或计算章节，未从正文补写原料清单。")
    elif not ingredients:
        issues.append("已识别原料或计算标题，但章节内容为空。")
    else:
        issues.append("原料与计算按原文保留，可能混含工具、介绍、可选项和替代分支；未完成食材语义核对。")
    if not step_sections:
        issues.append("未识别到操作章节，未补写烹饪步骤。")
    elif not steps:
        issues.append("已识别操作标题，但章节内容为空。")
    else:
        issues.append("步骤可能新增食材或用量，已保留步骤原文，仍需与原料及计算章节跨节核对。")
    if not serving_lines:
        issues.append("未识别到明确份数或每份基准原文，份数留空。")

    return {
        "title": title,
        "ingredients": ingredients,
        "steps": steps,
        "servings": "\n".join(serving_lines) or None,
        "ingredients_text": "\n\n".join(section["markdown"] for section in ingredient_sections),
        "steps_text": "\n\n".join(section["markdown"] for section in step_sections),
        "sections": sections,
        "completeness": {
            "title_found": bool(title),
            "ingredient_section_found": any(section["kind"] == "ingredients" for section in sections),
            "calculation_section_found": any(section["kind"] == "calculation" for section in sections),
            "ingredient_block_count": len(ingredients),
            "operation_section_found": bool(step_sections),
            "step_block_count": len(steps),
            "serving_statement_found": bool(serving_lines),
            "ingredient_inventory_verified": False,
            "all_quantities_verified": False,
            "tutorial_complete": None,
            "cooking_tested": False,
        },
        "issues": issues,
    }
