"""Test helpers: build a minimal people-CRM SQLite file for view/payload tests.

Only the columns the relationships view actually reads are created here; this
keeps the tests focused on view logic rather than the full CRM schema.
"""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE org (org_id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE org_domain (domain TEXT PRIMARY KEY, org_id TEXT, source TEXT);
CREATE TABLE person (
  person_id TEXT PRIMARY KEY, display_name TEXT, display_name_source TEXT,
  canonical_email TEXT, org_id TEXT
);
CREATE TABLE person_email (email TEXT PRIMARY KEY, person_id TEXT);
CREATE TABLE person_metric (
  person_id TEXT PRIMARY KEY, meeting_count INTEGER, one_on_one_count INTEGER,
  focused_count INTEGER, minutes_focused INTEGER
);
CREATE TABLE interaction (
  interaction_id TEXT PRIMARY KEY, source_system TEXT, direction TEXT,
  occurred_at TEXT, title TEXT, source_link TEXT, duration_minutes INTEGER
);
CREATE TABLE interaction_participant (interaction_id TEXT, email TEXT, role TEXT);
CREATE TABLE filter_rule (
  rule_type TEXT, value TEXT, label TEXT, source TEXT,
  PRIMARY KEY (rule_type, value)
);
"""


def build_min_crm_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def add_person(
    path: Path,
    person_id: str,
    name: str | None,
    email: str,
    org_id: str | None = None,
    meetings: int = 0,
    one_on_one: int = 0,
    focused: int = 0,
    minutes_focused: int = 0,
) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO person VALUES (?,?,?,?,?)",
        (person_id, name, "GMAIL", email, org_id),
    )
    conn.execute("INSERT INTO person_email VALUES (?,?)", (email, person_id))
    if meetings or one_on_one or focused or minutes_focused:
        conn.execute(
            "INSERT INTO person_metric VALUES (?,?,?,?,?)",
            (person_id, meetings, one_on_one, focused, minutes_focused),
        )
    conn.commit()
    conn.close()


def add_interaction(
    path: Path,
    interaction_id: str,
    email: str,
    source_system: str,
    direction: str | None,
    occurred_at: str,
    title: str = "",
    duration_minutes: int | None = None,
) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO interaction VALUES (?,?,?,?,?,?,?)",
        (interaction_id, source_system, direction, occurred_at, title, None, duration_minutes),
    )
    conn.execute(
        "INSERT INTO interaction_participant VALUES (?,?,?)",
        (interaction_id, email, "SENDER" if direction == "RECEIVED" else "ATTENDEE"),
    )
    conn.commit()
    conn.close()


def add_alias(path: Path, person_id: str, email: str) -> None:
    """Attach an additional email address to an existing person."""
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO person_email VALUES (?,?)", (email, person_id))
    conn.commit()
    conn.close()


def add_org(path: Path, org_id: str, name: str, *domains: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO org VALUES (?,?)", (org_id, name))
    for domain in domains:
        conn.execute(
            "INSERT INTO org_domain VALUES (?,?,?)", (domain, org_id, "USER")
        )
    conn.commit()
    conn.close()
