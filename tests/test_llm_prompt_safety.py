"""Shared LLM judgment-call helpers (ADR-0011 extracted these from
app/librarian_engine.py into app/llm_prompt_safety.py so app/room_ai.py
reuses the exact same implementation). tests/test_librarian_engine.py
already exercises `_new_prompt_nonce`/`_strip_boundary_token` indirectly
through the librarian's re-exported names; this file tests the shared
module directly, plus `extract_json_object` (also newly shared).
"""

from app.llm_prompt_safety import extract_json_object, new_prompt_nonce, strip_boundary_token, truncate


def test_new_prompt_nonce_is_unpredictable_and_differs_between_calls():
    n1 = new_prompt_nonce()
    n2 = new_prompt_nonce()
    assert n1 != n2
    assert len(n1) == 16
    assert all(c in "0123456789abcdef" for c in n1)


def test_strip_boundary_token_replaces_literal_occurrence():
    nonce = "cafebabedeadbeef"
    text = f"before {nonce} after"
    assert strip_boundary_token(text, nonce) == "before [boundary-token-removed] after"


def test_strip_boundary_token_handles_empty_and_none():
    assert strip_boundary_token("", "nonce") == ""
    assert strip_boundary_token(None, "nonce") is None


def test_truncate_leaves_short_text_unchanged():
    assert truncate("short", 100) == "short"


def test_truncate_cuts_long_text_and_marks_it():
    text = "x" * 200
    result = truncate(text, 50)
    assert result.startswith("x" * 50)
    assert "[truncated]" in result
    assert len(result) < len(text)


def test_extract_json_object_parses_plain_json():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_strips_code_fence():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_object_greedily_grabs_outermost_object_amid_commentary():
    content = 'Sure, here you go:\n{"a": 1, "b": {"c": 2}}\nHope that helps!'
    assert extract_json_object(content) == {"a": 1, "b": {"c": 2}}


def test_extract_json_object_returns_none_for_garbage():
    assert extract_json_object("this is not json at all") is None


def test_extract_json_object_returns_none_for_empty_string():
    assert extract_json_object("") is None


def test_extract_json_object_returns_none_for_json_array_not_object():
    assert extract_json_object("[1, 2, 3]") is None
