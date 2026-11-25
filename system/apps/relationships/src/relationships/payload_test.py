import sqlite3
from pathlib import Path

from relationships.filters_store import add_rule
from relationships.payload import build_person_detail
from relationships.payload import build_summary_payload
from relationships.testing import add_alias
from relationships.testing import add_interaction
from relationships.testing import add_org
from relationships.testing import add_person
from relationships.testing import build_min_crm_db


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "crm.db"
    build_min_crm_db(path)
    return path


def test_self_is_never_a_contact(tmp_path: Path) -> None:
    path = _db(tmp_path)
    # one real contact and one of the user's own historical addresses
    add_person(path, "per-1", "Real Person", "real@acme.com", meetings=2, one_on_one=1)
    add_person(path, "per-self", "Sam Rivera", "sam@old-employer.example.org")
    add_interaction(path, "i-self", "sam@old-employer.example.org", "GMAIL", "SENT", "2026-01-01T10:00:00Z")
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE interaction_participant SET role = 'SELF' WHERE interaction_id = 'i-self'")
    conn.commit()
    conn.close()
    payload = build_summary_payload(path)
    emails = {p["email"] for p in payload["people"]}
    assert "real@acme.com" in emails
    assert "sam@old-employer.example.org" not in emails


def test_email_counts_and_direction(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_person(path, "per-1", "Dana", "dana@acme.com")
    add_interaction(path, "i1", "dana@acme.com", "GMAIL", "RECEIVED", "2026-01-01T10:00:00Z")
    add_interaction(path, "i2", "dana@acme.com", "GMAIL", "RECEIVED", "2026-02-01T10:00:00Z")
    add_interaction(path, "i3", "dana@acme.com", "GMAIL", "SENT", "2026-03-01T10:00:00Z")
    person = next(p for p in build_summary_payload(path)["people"] if p["email"] == "dana@acme.com")
    assert person["email_total"] == 3
    assert person["email_received"] == 2
    assert person["email_sent"] == 1
    assert person["channel"] == "email"  # no meetings
    assert person["last_contact"] == "2026-03-01"


def test_unreplied_sender_is_one_way_and_left_out_of_stats(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_person(path, "per-nl", "Never Replied", "cold@news.com")
    for i in range(3):
        add_interaction(path, f"n{i}", "cold@news.com", "GMAIL", "RECEIVED", f"2026-0{i+1}-01T10:00:00Z")
    payload = build_summary_payload(path)
    person = next(p for p in payload["people"] if p["email"] == "cold@news.com")
    assert person["one_way"] is True
    # present in the raw list (with a flag) but not counted in the default population
    assert payload["stats"]["people"] == 0
    assert payload["stats"]["one_way"] == 1


def test_replied_or_met_person_is_not_one_way(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_person(path, "per-met", "Met Person", "met@acme.com", meetings=2)
    add_person(path, "per-reply", "Replied To", "reply@acme.com")
    add_interaction(path, "r-in", "reply@acme.com", "GMAIL", "RECEIVED", "2026-01-01T10:00:00Z")
    add_interaction(path, "r-out", "reply@acme.com", "GMAIL", "SENT", "2026-01-02T10:00:00Z")
    by_email = {p["email"]: p for p in build_summary_payload(path)["people"]}
    assert by_email["met@acme.com"]["one_way"] is False       # met, no email
    assert by_email["reply@acme.com"]["one_way"] is False      # emailed and replied


def test_filter_rule_marks_person_excluded(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_person(path, "per-1", "Kept", "kept@acme.com", meetings=1)
    add_person(path, "per-2", "Hidden Co", "x@blocked.com", meetings=1)
    add_person(path, "per-3", "Hidden Me", "hide@acme.com", meetings=1)
    add_rule("DOMAIN", "blocked.com", "Blocked", db_path=path)
    add_rule("EMAIL", "hide@acme.com", "nope", db_path=path)
    by_email = {p["email"]: p for p in build_summary_payload(path)["people"]}
    assert by_email["kept@acme.com"]["excluded"] is False
    assert by_email["x@blocked.com"]["excluded"] is True
    assert by_email["hide@acme.com"]["excluded"] is True
    # stats count only visible (non-excluded) people
    assert build_summary_payload(path)["stats"]["people"] == 1


def test_strength_orders_meetings_over_email(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_person(path, "per-mtg", "Meeter", "m@acme.com", meetings=5, one_on_one=5, focused=5)
    add_person(path, "per-mail", "Mailer", "e@acme.com")
    for i in range(10):
        add_interaction(path, f"e{i}", "e@acme.com", "GMAIL", "RECEIVED", f"2026-01-{i+1:02d}T10:00:00Z")
    people = build_summary_payload(path)["people"]
    assert people[0]["email"] == "m@acme.com"  # face time outranks email volume


def test_org_grouping_and_named_flag(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_org(path, "org-1", "Acme Corp", "acme.com")
    add_person(path, "per-1", "A", "a@acme.com", meetings=1)
    add_person(path, "per-2", "B", "b@nowhere.io", meetings=1)
    by_email = {p["email"]: p for p in build_summary_payload(path)["people"]}
    assert by_email["a@acme.com"]["org"] == "Acme Corp"
    assert by_email["a@acme.com"]["org_named"] is True
    assert by_email["b@nowhere.io"]["org"] == "nowhere.io"  # falls back to domain
    assert by_email["b@nowhere.io"]["org_named"] is False


def test_primary_company_follows_most_recent_work_address(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_org(path, "org-old", "Old Co", "oldco.org")
    add_org(path, "org-new", "New Co", "newco.org")
    # canonical is the stale address; the person now uses newco.org, plus gmail
    add_person(path, "per-1", "Mover", "person@oldco.org", meetings=1)
    add_alias(path, "per-1", "person@newco.org")
    add_alias(path, "per-1", "mover@gmail.com")
    add_interaction(path, "old", "person@oldco.org", "GOOGLE_CALENDAR", None, "2023-01-01T10:00:00Z")
    add_interaction(path, "gm", "mover@gmail.com", "GMAIL", "SENT", "2026-06-01T10:00:00Z")
    add_interaction(path, "new", "person@newco.org", "GMAIL", "SENT", "2026-06-15T10:00:00Z")
    person = next(p for p in build_summary_payload(path)["people"] if p["person_id"] == "per-1")
    # most recent *work* address wins over both the stale one and the newer gmail
    assert person["org"] == "New Co"
    assert person["domain"] == "newco.org"


def test_scheduling_emails_do_not_count(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_person(path, "per-1", "Sched", "s@acme.com", meetings=1)
    add_interaction(path, "real", "s@acme.com", "GMAIL", "SENT", "2026-01-01T10:00:00Z", title="Re: the proposal")
    add_interaction(path, "inv", "s@acme.com", "GMAIL", "RECEIVED", "2026-01-02T10:00:00Z", title="Invitation: Sync @ Mon")
    add_interaction(path, "acc", "s@acme.com", "GMAIL", "RECEIVED", "2026-01-03T10:00:00Z", title="Accepted: Sync")
    person = next(p for p in build_summary_payload(path)["people"] if p["person_id"] == "per-1")
    assert person["email_total"] == 1  # only the real message, not the 2 calendar notices


def test_email_strength_is_capped(tmp_path: Path) -> None:
    path = _db(tmp_path)
    # a burst of correspondence, no meetings
    add_person(path, "per-mail", "Chatty", "chatty@acme.com")
    for i in range(50):
        add_interaction(path, f"c{i}", "chatty@acme.com", "GMAIL", "SENT", f"2026-01-{i % 28 + 1:02d}T10:00:00Z", title="hi")
    # a single 1:1 meeting, no email
    add_person(path, "per-mtg", "Meeter", "meeter@acme.com", meetings=1, one_on_one=1, focused=1)
    people = build_summary_payload(path)["people"]
    strengths = {p["person_id"]: p["strength"] for p in people}
    assert strengths["per-mail"] <= 6.0  # email contribution is capped
    assert strengths["per-mtg"] > strengths["per-mail"]  # one real meeting outranks 50 emails


def test_person_detail_timeline(tmp_path: Path) -> None:
    path = _db(tmp_path)
    add_person(path, "per-1", "Dana", "dana@acme.com")
    add_interaction(path, "i1", "dana@acme.com", "GMAIL", "RECEIVED", "2026-01-01T10:00:00Z", title="hi")
    add_interaction(path, "i2", "dana@acme.com", "GOOGLE_CALENDAR", None, "2026-02-01T10:00:00Z", title="1:1", duration_minutes=30)
    detail = build_person_detail("per-1", path)
    assert len(detail["timeline"]) == 2
    # newest first
    assert detail["timeline"][0]["date"] == "2026-02-01"
    assert detail["timeline"][0]["type"] == "meeting"


def test_summary_renders_empty_before_the_first_sync(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "crm.db"
    payload = build_summary_payload(path)
    assert payload["people"] == []
    assert path.exists()
