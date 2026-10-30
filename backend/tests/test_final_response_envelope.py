"""A real final answer duplicated prose before its JSON envelope."""
import json

from app import providers


def test_duplicate_body_before_final_json_is_recovered_without_duplicate_rendering():
    body = '没有依据。已发布知识库没有隔夜燕麦条目。\n保留未知，不编造来源。'
    envelope = {'text': body, 'needs_input': False, 'input_prompt': '', 'citations': []}
    value = providers._parse_final_content(body + '\n\n' + json.dumps(envelope, ensure_ascii=False))
    assert value == envelope


def test_nonduplicate_prefix_and_literal_json_are_not_silently_removed():
    text = '用户给的示例是 {"text":"示例"}，这只是解释。'
    assert providers._parse_final_content(text)['text'] == text
    different = '额外事实应保留。\n' + json.dumps({'text': '另一段正文'}, ensure_ascii=False)
    assert providers._parse_final_content(different)['text'] == different


def test_json_with_unknown_business_fields_is_not_extracted_from_prose():
    text = '正文。\n' + json.dumps({'text': '正文。', 'data': {'ingredients': ['奶粉']}}, ensure_ascii=False)
    assert providers._parse_final_content(text)['text'] == text


def test_valid_json_stays_structured_including_citations_and_questions():
    value = {'text': '需要补充购买日期。', 'needs_input': True, 'input_prompt': '哪天买的？', 'citations': []}
    assert providers._parse_final_content(json.dumps(value, ensure_ascii=False)) == value
