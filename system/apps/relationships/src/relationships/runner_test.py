import json
import re
from pathlib import Path

import pytest

from relationships import runner
from relationships.filters_store import add_rule
from relationships.filters_store import read_rules
from relationships.payload import build_summary_payload
from relationships.runner import render_page
from relationships.testing import add_person
from relationships.testing import build_min_crm_db

# A meeting title or attendee name that closes the data element and injects
# markup after it. Anyone who can send the user a calendar invite controls
# these strings, so the page must survive them.
BREAKOUT = '</script><img src=x onerror="alert(1)">'


def _data_block(page: str) -> str:
    match = re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.DOTALL)
    assert match is not None, "the data element is missing or was closed early"
    return match.group(1)


def test_a_hostile_name_cannot_close_the_data_element() -> None:
    page = render_page({"people": [{"name": BREAKOUT, "email": "a@b.com"}]})
    # The injected markup must not appear as markup anywhere in the document.
    assert "<img src=x" not in page
    # The literal closing tag must not survive into the page at all.
    assert "</script><img" not in page


def test_the_payload_still_round_trips_through_the_escaping() -> None:
    payload = {"people": [{"name": BREAKOUT, "email": "a@b.com"}], "stats": {"people": 1}}
    # What the browser's JSON.parse sees is the data element's text content.
    assert json.loads(_data_block(render_page(payload))) == payload


def test_line_separators_are_escaped() -> None:
    # U+2028/U+2029 are valid in JSON strings but terminate a line in older
    # JavaScript parsers, which would break the surrounding <script> block.
    page = render_page({"people": [{"name": "a\u2028b\u2029c"}]})
    assert "\u2028" not in page
    assert "\u2029" not in page


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "crm.db"
    build_min_crm_db(path)
    add_person(path, "per-1", "Dana Example", "dana@acme.com", meetings=2, one_on_one=1)
    # Reading the rules creates the view's own filter-category table, a one-time
    # change to the store; a real store has it already.
    read_rules(path)
    return path


def test_an_unchanged_store_serves_the_kept_page_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _db(tmp_path)
    builds: list[Path] = []

    def counting_build(db_path: Path) -> dict:
        builds.append(db_path)
        return build_summary_payload(db_path)

    monkeypatch.setattr(runner, "build_summary_payload", counting_build)
    first = runner.summary_page(path)
    second = runner.summary_page(path)
    assert second == first
    assert len(builds) == 1


def test_a_new_contact_shows_up_once_the_store_changes(tmp_path: Path) -> None:
    path = _db(tmp_path)
    assert "Sam Newcomer" not in runner.summary_page(path)
    add_person(path, "per-2", "Sam Newcomer", "sam@acme.com", meetings=1)
    assert "Sam Newcomer" in runner.summary_page(path)


def test_a_filter_edit_shows_up_on_the_next_open(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before = json.loads(_data_block(runner.summary_page(path)))
    assert before["stats"]["excluded"] == 0
    add_rule("DOMAIN", "acme.com", None, None, db_path=path)
    after = json.loads(_data_block(runner.summary_page(path)))
    assert after["stats"]["excluded"] == 1
