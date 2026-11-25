"""Read/write exclusion rules for the relationships view.

Rules live in the CRM store's canonical `filter_rule` table. Each rule also
carries a *reason* (spam / personal / other) so a personal contact you set aside
is not lumped in with junk; because the shared `FilterRule` model forbids extra
fields, the reason is kept in a companion table this view owns.

Uses short-lived connections with a busy timeout so edits are safe even while a
long `people-crm sync` is writing to the same database file.
"""

import sqlite3
from pathlib import Path
from typing import Any

_DB_PATH = Path("runtime/people-crm/crm.db")

VALID_CATEGORIES = ("SPAM", "PERSONAL", "OTHER")
_DEFAULT_CATEGORY = "OTHER"

_CATEGORY_TABLE = """
CREATE TABLE IF NOT EXISTS relationships_filter_category (
  rule_type TEXT NOT NULL,
  value TEXT NOT NULL,
  category TEXT NOT NULL,
  PRIMARY KEY (rule_type, value)
)
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute(_CATEGORY_TABLE)
    return conn


def _norm_category(category: str | None) -> str:
    value = (category or "").strip().upper()
    return value if value in VALID_CATEGORIES else _DEFAULT_CATEGORY


def read_rules(db_path: Path = _DB_PATH) -> dict[str, list[dict[str, Any]]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT r.rule_type, r.value, r.label, r.source,
                   COALESCE(c.category, ?) AS category
            FROM filter_rule r
            LEFT JOIN relationships_filter_category c
              ON c.rule_type = r.rule_type AND c.value = r.value
            """,
            (_DEFAULT_CATEGORY,),
        ).fetchall()
    finally:
        conn.close()
    domains = [dict(r) for r in rows if r["rule_type"] == "DOMAIN"]
    emails = [dict(r) for r in rows if r["rule_type"] == "EMAIL"]
    return {"domains": domains, "emails": emails}


def add_rule(
    rule_type: str,
    value: str,
    label: str | None,
    category: str | None = None,
    db_path: Path = _DB_PATH,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO filter_rule (rule_type, value, label, source)
            VALUES (?, ?, ?, 'USER')
            ON CONFLICT(rule_type, value) DO UPDATE SET label = excluded.label
            """,
            (rule_type, value, label),
        )
        conn.execute(
            """
            INSERT INTO relationships_filter_category (rule_type, value, category)
            VALUES (?, ?, ?)
            ON CONFLICT(rule_type, value) DO UPDATE SET category = excluded.category
            """,
            (rule_type, value, _norm_category(category)),
        )
        conn.commit()
    finally:
        conn.close()


def set_category(rule_type: str, value: str, category: str, db_path: Path = _DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO relationships_filter_category (rule_type, value, category)
            VALUES (?, ?, ?)
            ON CONFLICT(rule_type, value) DO UPDATE SET category = excluded.category
            """,
            (rule_type, value, _norm_category(category)),
        )
        conn.commit()
    finally:
        conn.close()


def remove_rule(rule_type: str, value: str, db_path: Path = _DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "DELETE FROM filter_rule WHERE rule_type = ? AND value = ?", (rule_type, value)
        )
        conn.execute(
            "DELETE FROM relationships_filter_category WHERE rule_type = ? AND value = ?",
            (rule_type, value),
        )
        conn.commit()
    finally:
        conn.close()
