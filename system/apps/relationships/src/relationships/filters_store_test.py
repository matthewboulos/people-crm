from pathlib import Path

from relationships.filters_store import add_rule
from relationships.filters_store import read_rules
from relationships.filters_store import remove_rule
from relationships.filters_store import set_category
from relationships.testing import build_min_crm_db


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "crm.db"
    build_min_crm_db(path)
    return path


def test_add_read_remove_domain_round_trip(tmp_path: Path) -> None:
    path = _db(tmp_path)
    assert read_rules(path) == {"domains": [], "emails": []}

    add_rule("DOMAIN", "acme.com", "Acme", db_path=path)
    rules = read_rules(path)
    assert [d["value"] for d in rules["domains"]] == ["acme.com"]
    assert rules["domains"][0]["source"] == "USER"

    remove_rule("DOMAIN", "acme.com", path)
    assert read_rules(path)["domains"] == []


def test_add_email_rule(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_rule("EMAIL", "spam@acme.com", "nope", db_path=path)
    rules = read_rules(path)
    assert [e["value"] for e in rules["emails"]] == ["spam@acme.com"]


def test_add_is_idempotent_upsert(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_rule("DOMAIN", "acme.com", "first", "OTHER", path)
    add_rule("DOMAIN", "acme.com", "second", "OTHER", path)
    rules = read_rules(path)
    assert len(rules["domains"]) == 1
    assert rules["domains"][0]["label"] == "second"


def test_rule_carries_category_defaulting_to_other(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_rule("EMAIL", "spam@x.com", "junk", "SPAM", path)
    add_rule("EMAIL", "friend@x.com", "a friend", "PERSONAL", path)
    add_rule("EMAIL", "misc@x.com", None, None, path)  # unspecified -> OTHER
    by_value = {e["value"]: e["category"] for e in read_rules(path)["emails"]}
    assert by_value["spam@x.com"] == "SPAM"
    assert by_value["friend@x.com"] == "PERSONAL"
    assert by_value["misc@x.com"] == "OTHER"


def test_set_category_reclassifies_an_existing_rule(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_rule("DOMAIN", "acme.com", "Acme", "OTHER", path)
    set_category("DOMAIN", "acme.com", "PERSONAL", path)
    assert read_rules(path)["domains"][0]["category"] == "PERSONAL"


def test_invalid_category_falls_back_to_other(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_rule("DOMAIN", "acme.com", "Acme", "BOGUS", path)
    assert read_rules(path)["domains"][0]["category"] == "OTHER"


def test_remove_clears_category_overlay(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_rule("DOMAIN", "acme.com", "Acme", "SPAM", path)
    remove_rule("DOMAIN", "acme.com", path)
    # re-adding without a category should not resurrect the old SPAM label
    add_rule("DOMAIN", "acme.com", "Acme", None, path)
    assert read_rules(path)["domains"][0]["category"] == "OTHER"
