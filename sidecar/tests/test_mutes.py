"""Unit tests for the per-feed author/tag mute filter (Part A)."""
from app.routes.entries import _apply_mutes, _entry_tag_names, _is_muted


def _e(eid, author=None, tags=None):
    return {"id": eid, "author": author, "tags": tags or []}


def test_is_muted_author_case_insensitive():
    e = _e(1, author="Josh Blackman")
    assert _is_muted(e, ["josh blackman"], [])      # casefold match
    assert _is_muted(e, ["  JOSH BLACKMAN  "], [])  # whitespace + case
    assert not _is_muted(e, ["someone else"], [])


def test_is_muted_tag_case_insensitive():
    e = _e(1, tags=["Crypto", "News"])
    assert _is_muted(e, [], ["crypto"])
    assert not _is_muted(e, [], ["politics"])


def test_apply_mutes_both_dimensions():
    es = [
        _e(1, author="Josh Blackman", tags=["law"]),
        _e(2, author="Eugene Volokh", tags=["speech"]),
        _e(3, author="josh blackman"),              # dupe author, different case
        _e(4, author="", tags=["Crypto"]),
    ]
    assert [e["id"] for e in _apply_mutes(es, ["Josh Blackman"], [])] == [2, 4]
    assert [e["id"] for e in _apply_mutes(es, [], ["crypto"])] == [1, 2, 3]
    assert [e["id"] for e in _apply_mutes(es, ["eugene volokh"], ["law"])] == [3, 4]


def test_apply_mutes_empty_is_noop():
    es = [_e(1, author="A"), _e(2, author="B")]
    assert _apply_mutes(es, [], []) is es           # same object, no copy


def test_entry_tag_names_handles_strings_and_objects():
    e = _e(1, tags=["plain", {"title": "Titled"}, {"name": "Named"}, {"x": "y"}])
    names = _entry_tag_names(e)
    assert names == ["plain", "Titled", "Named"]     # malformed object skipped


def test_empty_author_not_muted():
    # An entry with no author must not match an author mute list.
    assert not _is_muted(_e(1, author=""), ["", "X"], [])
