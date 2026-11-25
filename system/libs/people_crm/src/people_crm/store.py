import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from pydantic import Field, PrivateAttr

from people_crm.data_types import (
    BackfillStatus,
    CategoryFeedback,
    EmailIdentity,
    FilterRule,
    IdentityOverride,
    Interaction,
    InteractionParticipant,
    Org,
    OrgDomain,
    Person,
    PersonEmailLink,
    PersonMetric,
    PersonUserData,
    SourceSystem,
)
from people_crm.errors import CrmStoreError
from people_crm.interfaces import CrmStoreInterface
from people_crm.primitives import EmailAddress, PersonId

# How long a write waits for a reader to let go before failing. The relationships
# view copies the whole store to build its page, which can take well over
# sqlite's 5-second default, and a sync must outlast that rather than abort.
BUSY_TIMEOUT_SECONDS: Final[float] = 60.0

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS email_identity (
  email TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  observed_name TEXT,
  observed_name_source TEXT,
  is_free_email_domain INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interaction (
  interaction_id TEXT PRIMARY KEY,
  interaction_type TEXT NOT NULL,
  source_system TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  source_link TEXT,
  occurred_at TEXT NOT NULL,
  duration_minutes INTEGER,
  title TEXT NOT NULL,
  recurring_series_ref TEXT,
  direction TEXT,
  raw_payload TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  UNIQUE (source_system, source_ref)
);

CREATE TABLE IF NOT EXISTS interaction_participant (
  interaction_id TEXT NOT NULL REFERENCES interaction(interaction_id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  role TEXT NOT NULL,
  response_status TEXT,
  PRIMARY KEY (interaction_id, email, role)
);
CREATE INDEX IF NOT EXISTS idx_participant_email ON interaction_participant(email);

CREATE TABLE IF NOT EXISTS org (
  org_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_domain (
  domain TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
  source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person (
  person_id TEXT PRIMARY KEY,
  canonical_email TEXT NOT NULL,
  display_name TEXT,
  display_name_source TEXT,
  org_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_email (
  email TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  merge_reason TEXT NOT NULL,
  merge_confidence REAL,
  merged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_person_email_person ON person_email(person_id);

CREATE TABLE IF NOT EXISTS person_metric (
  person_id TEXT PRIMARY KEY,
  meeting_count INTEGER NOT NULL,
  one_on_one_count INTEGER NOT NULL,
  focused_count INTEGER NOT NULL,
  minutes_total INTEGER NOT NULL,
  minutes_focused INTEGER NOT NULL,
  first_interaction_date TEXT,
  last_interaction_date TEXT,
  days_since_last INTEGER,
  is_group_only INTEGER NOT NULL,
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_user_data (
  person_id TEXT PRIMARY KEY,
  tier TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_tag (
  person_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  PRIMARY KEY (person_id, tag)
);

CREATE TABLE IF NOT EXISTS category_feedback (
  feedback_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  signal TEXT NOT NULL,
  note TEXT,
  category TEXT,
  judge_verdict TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_person ON category_feedback(person_id);

CREATE TABLE IF NOT EXISTS identity_override (
  email TEXT PRIMARY KEY,
  override_type TEXT NOT NULL,
  forced_person_id TEXT,
  reason TEXT,
  backfill_status TEXT NOT NULL,
  backfilled_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filter_rule (
  rule_type TEXT NOT NULL,
  value TEXT NOT NULL,
  label TEXT,
  source TEXT NOT NULL,
  PRIMARY KEY (rule_type, value)
);
"""


@pure
def _dt_to_str(value: datetime) -> str:
    return value.isoformat()


@pure
def _str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pure
def _date_to_str(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


class SqliteCrmStore(CrmStoreInterface):
    """SQLite-backed implementation of the three-layer CRM store."""

    database_path: Path = Field(
        frozen=True, description="Path to the SQLite database file"
    )
    _connection: sqlite3.Connection | None = PrivateAttr(default=None)

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise CrmStoreError("Store is not initialized; call initialize() first")
        return self._connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(str(self.database_path), timeout=BUSY_TIMEOUT_SECONDS)
        except sqlite3.Error as e:
            raise CrmStoreError(f"Cannot open database: {self.database_path}") from e
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA)
        connection.commit()
        self._connection = connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # --- Source layer ---

    def upsert_email_identity(self, identity: EmailIdentity) -> None:
        # Widen the first/last-seen window rather than overwriting it, and only
        # overwrite the observed name when the incoming one is present. The
        # window is widened by comparing timezone-aware instants in Python:
        # occurred_at preserves each event's original UTC offset, so a raw
        # lexicographic MIN()/MAX() over the stored ISO strings would order
        # equal-instant / different-offset times incorrectly.
        existing_cursor = self._conn.execute(
            """
            SELECT observed_name, observed_name_source, first_seen_at, last_seen_at
            FROM email_identity WHERE email = ?
            """,
            (str(identity.email),),
        )
        existing = existing_cursor.fetchone()

        incoming_name = (
            str(identity.observed_name) if identity.observed_name is not None else None
        )
        incoming_name_source = _enum_value(identity.observed_name_source)
        if existing is not None:
            first_seen = min(
                _str_to_dt(existing["first_seen_at"]), identity.first_seen_at
            )
            last_seen = max(_str_to_dt(existing["last_seen_at"]), identity.last_seen_at)
            observed_name = (
                incoming_name
                if incoming_name is not None
                else existing["observed_name"]
            )
            observed_name_source = (
                incoming_name_source
                if incoming_name_source is not None
                else existing["observed_name_source"]
            )
        else:
            first_seen = identity.first_seen_at
            last_seen = identity.last_seen_at
            observed_name = incoming_name
            observed_name_source = incoming_name_source

        self._conn.execute(
            """
            INSERT INTO email_identity
                (email, domain, observed_name, observed_name_source,
                 is_free_email_domain, first_seen_at, last_seen_at)
            VALUES (:email, :domain, :observed_name, :observed_name_source,
                    :is_free_email_domain, :first_seen_at, :last_seen_at)
            ON CONFLICT(email) DO UPDATE SET
                observed_name = excluded.observed_name,
                observed_name_source = excluded.observed_name_source,
                first_seen_at = excluded.first_seen_at,
                last_seen_at = excluded.last_seen_at
            """,
            {
                "email": str(identity.email),
                "domain": str(identity.domain),
                "observed_name": observed_name,
                "observed_name_source": observed_name_source,
                "is_free_email_domain": int(identity.is_free_email_domain),
                "first_seen_at": _dt_to_str(first_seen),
                "last_seen_at": _dt_to_str(last_seen),
            },
        )
        self._conn.commit()

    def upsert_interaction(self, interaction: Interaction) -> None:
        # The natural upsert key is (source_system, source_ref); a re-run
        # preserves the existing interaction_id and first ingested_at so
        # downstream references stay stable.
        cursor = self._conn.execute(
            "SELECT interaction_id, ingested_at FROM interaction WHERE source_system = ? AND source_ref = ?",
            (_enum_value(interaction.source_system), str(interaction.source_ref)),
        )
        existing = cursor.fetchone()
        if existing is not None:
            stable_id = existing["interaction_id"]
            stable_ingested_at = existing["ingested_at"]
        else:
            stable_id = str(interaction.interaction_id)
            stable_ingested_at = _dt_to_str(interaction.ingested_at)

        self._conn.execute(
            """
            INSERT INTO interaction
                (interaction_id, interaction_type, source_system, source_ref, source_link,
                 occurred_at, duration_minutes, title, recurring_series_ref, direction,
                 raw_payload, ingested_at)
            VALUES (:interaction_id, :interaction_type, :source_system, :source_ref, :source_link,
                    :occurred_at, :duration_minutes, :title, :recurring_series_ref, :direction,
                    :raw_payload, :ingested_at)
            ON CONFLICT(interaction_id) DO UPDATE SET
                source_link = excluded.source_link,
                occurred_at = excluded.occurred_at,
                duration_minutes = excluded.duration_minutes,
                title = excluded.title,
                recurring_series_ref = excluded.recurring_series_ref,
                direction = excluded.direction,
                raw_payload = excluded.raw_payload
            """,
            {
                "interaction_id": stable_id,
                "interaction_type": _enum_value(interaction.interaction_type),
                "source_system": _enum_value(interaction.source_system),
                "source_ref": str(interaction.source_ref),
                "source_link": interaction.source_link,
                "occurred_at": _dt_to_str(interaction.occurred_at),
                "duration_minutes": int(interaction.duration_minutes)
                if interaction.duration_minutes is not None
                else None,
                "title": interaction.title,
                "recurring_series_ref": interaction.recurring_series_ref,
                "direction": _enum_value(interaction.direction),
                "raw_payload": interaction.raw_payload,
                "ingested_at": stable_ingested_at,
            },
        )
        self._conn.execute(
            "DELETE FROM interaction_participant WHERE interaction_id = ?", (stable_id,)
        )
        self._conn.executemany(
            """
            INSERT INTO interaction_participant (interaction_id, email, role, response_status)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    stable_id,
                    str(p.email),
                    _enum_value(p.role),
                    _enum_value(p.response_status),
                )
                for p in interaction.participants
            ],
        )
        self._conn.commit()

    def get_interaction_by_source_ref(
        self, source_system: SourceSystem, source_ref: str
    ) -> Interaction | None:
        cursor = self._conn.execute(
            "SELECT * FROM interaction WHERE source_system = ? AND source_ref = ?",
            (_enum_value(source_system), source_ref),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_interaction(row)

    def get_all_interactions(self) -> list[Interaction]:
        cursor = self._conn.execute("SELECT * FROM interaction")
        return [self._row_to_interaction(row) for row in cursor.fetchall()]

    def _row_to_interaction(self, row: sqlite3.Row) -> Interaction:
        participant_cursor = self._conn.execute(
            "SELECT email, role, response_status FROM interaction_participant WHERE interaction_id = ?",
            (row["interaction_id"],),
        )
        participants = tuple(
            InteractionParticipant.model_validate(dict(p))
            for p in participant_cursor.fetchall()
        )
        data = dict(row)
        data["participants"] = participants
        return Interaction.model_validate(data)

    def get_all_email_identities(self) -> list[EmailIdentity]:
        cursor = self._conn.execute("SELECT * FROM email_identity")
        return [EmailIdentity.model_validate(dict(row)) for row in cursor.fetchall()]

    # --- Derived layer ---

    def replace_person_graph(
        self, persons: Sequence[Person], links: Sequence[PersonEmailLink]
    ) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM person")
            self._conn.execute("DELETE FROM person_email")
            self._conn.executemany(
                """
                INSERT INTO person
                    (person_id, canonical_email, display_name, display_name_source,
                     org_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(person.person_id),
                        str(person.canonical_email),
                        str(person.display_name)
                        if person.display_name is not None
                        else None,
                        _enum_value(person.display_name_source),
                        str(person.org_id) if person.org_id is not None else None,
                        _dt_to_str(person.created_at),
                        _dt_to_str(person.updated_at),
                    )
                    for person in persons
                ],
            )
            self._conn.executemany(
                """
                INSERT INTO person_email (email, person_id, merge_reason, merge_confidence, merged_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(link.email),
                        str(link.person_id),
                        _enum_value(link.merge_reason),
                        float(link.merge_confidence)
                        if link.merge_confidence is not None
                        else None,
                        _dt_to_str(link.merged_at),
                    )
                    for link in links
                ],
            )

    def get_all_persons(self) -> list[Person]:
        cursor = self._conn.execute("SELECT * FROM person")
        return [Person.model_validate(dict(row)) for row in cursor.fetchall()]

    def get_all_person_email_links(self) -> list[PersonEmailLink]:
        cursor = self._conn.execute("SELECT * FROM person_email")
        return [PersonEmailLink.model_validate(dict(row)) for row in cursor.fetchall()]

    def get_person_ids_with_user_data(self) -> set[PersonId]:
        cursor = self._conn.execute(
            """
            SELECT person_id FROM person_user_data
            UNION SELECT person_id FROM person_tag
            UNION SELECT person_id FROM category_feedback
            """
        )
        return {PersonId(row["person_id"]) for row in cursor.fetchall()}

    def upsert_org(self, org: Org) -> None:
        self._conn.execute(
            """
            INSERT INTO org (org_id, name, created_at, updated_at)
            VALUES (:org_id, :name, :created_at, :updated_at)
            ON CONFLICT(org_id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
            """,
            {
                "org_id": str(org.org_id),
                "name": str(org.name),
                "created_at": _dt_to_str(org.created_at),
                "updated_at": _dt_to_str(org.updated_at),
            },
        )
        self._conn.commit()

    def upsert_org_domain(self, org_domain: OrgDomain) -> None:
        self._conn.execute(
            """
            INSERT INTO org_domain (domain, org_id, source)
            VALUES (:domain, :org_id, :source)
            ON CONFLICT(domain) DO UPDATE SET org_id = excluded.org_id, source = excluded.source
            """,
            {
                "domain": str(org_domain.domain),
                "org_id": str(org_domain.org_id),
                "source": _enum_value(org_domain.source),
            },
        )
        self._conn.commit()

    def get_all_orgs(self) -> list[Org]:
        cursor = self._conn.execute("SELECT * FROM org")
        return [Org.model_validate(dict(row)) for row in cursor.fetchall()]

    def get_all_org_domains(self) -> list[OrgDomain]:
        cursor = self._conn.execute("SELECT * FROM org_domain")
        return [OrgDomain.model_validate(dict(row)) for row in cursor.fetchall()]

    def replace_person_metrics(self, metrics: Sequence[PersonMetric]) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM person_metric")
            self._conn.executemany(
                """
                INSERT INTO person_metric
                    (person_id, meeting_count, one_on_one_count, focused_count, minutes_total,
                     minutes_focused, first_interaction_date, last_interaction_date,
                     days_since_last, is_group_only, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(metric.person_id),
                        int(metric.meeting_count),
                        int(metric.one_on_one_count),
                        int(metric.focused_count),
                        int(metric.minutes_total),
                        int(metric.minutes_focused),
                        _date_to_str(metric.first_interaction_date),
                        _date_to_str(metric.last_interaction_date),
                        int(metric.days_since_last)
                        if metric.days_since_last is not None
                        else None,
                        int(metric.is_group_only),
                        _dt_to_str(metric.computed_at),
                    )
                    for metric in metrics
                ],
            )

    def get_all_person_metrics(self) -> list[PersonMetric]:
        cursor = self._conn.execute("SELECT * FROM person_metric")
        return [PersonMetric.model_validate(dict(row)) for row in cursor.fetchall()]

    # --- User-data layer ---

    def get_all_identity_overrides(self) -> list[IdentityOverride]:
        cursor = self._conn.execute("SELECT * FROM identity_override")
        return [IdentityOverride.model_validate(dict(row)) for row in cursor.fetchall()]

    def upsert_identity_override(self, override: IdentityOverride) -> None:
        self._conn.execute(
            """
            INSERT INTO identity_override
                (email, override_type, forced_person_id, reason, backfill_status, backfilled_at, created_at)
            VALUES (:email, :override_type, :forced_person_id, :reason, :backfill_status,
                    :backfilled_at, :created_at)
            ON CONFLICT(email) DO UPDATE SET
                override_type = excluded.override_type,
                forced_person_id = excluded.forced_person_id,
                reason = excluded.reason,
                backfill_status = excluded.backfill_status,
                backfilled_at = excluded.backfilled_at
            """,
            {
                "email": str(override.email),
                "override_type": _enum_value(override.override_type),
                "forced_person_id": str(override.forced_person_id)
                if override.forced_person_id is not None
                else None,
                "reason": override.reason,
                "backfill_status": _enum_value(override.backfill_status),
                "backfilled_at": _dt_to_str(override.backfilled_at)
                if override.backfilled_at is not None
                else None,
                "created_at": _dt_to_str(override.created_at),
            },
        )
        self._conn.commit()

    def set_override_backfill_status(
        self,
        email: EmailAddress,
        status: BackfillStatus,
        backfilled_at: datetime | None,
    ) -> None:
        self._conn.execute(
            "UPDATE identity_override SET backfill_status = ?, backfilled_at = ? WHERE email = ?",
            (
                _enum_value(status),
                _dt_to_str(backfilled_at) if backfilled_at is not None else None,
                str(email),
            ),
        )
        self._conn.commit()

    def get_all_filter_rules(self) -> list[FilterRule]:
        cursor = self._conn.execute("SELECT * FROM filter_rule")
        return [FilterRule.model_validate(dict(row)) for row in cursor.fetchall()]

    def upsert_filter_rule(self, rule: FilterRule) -> None:
        self._conn.execute(
            """
            INSERT INTO filter_rule (rule_type, value, label, source)
            VALUES (:rule_type, :value, :label, :source)
            ON CONFLICT(rule_type, value) DO UPDATE SET label = excluded.label, source = excluded.source
            """,
            {
                "rule_type": _enum_value(rule.rule_type),
                "value": rule.value,
                "label": rule.label,
                "source": _enum_value(rule.source),
            },
        )
        self._conn.commit()

    def get_person_user_data(self, person_id: PersonId) -> PersonUserData | None:
        cursor = self._conn.execute(
            "SELECT * FROM person_user_data WHERE person_id = ?", (str(person_id),)
        )
        row = cursor.fetchone()
        return PersonUserData.model_validate(dict(row)) if row is not None else None

    def upsert_person_user_data(self, user_data: PersonUserData) -> None:
        self._conn.execute(
            """
            INSERT INTO person_user_data (person_id, tier, notes, updated_at)
            VALUES (:person_id, :tier, :notes, :updated_at)
            ON CONFLICT(person_id) DO UPDATE SET
                tier = excluded.tier, notes = excluded.notes, updated_at = excluded.updated_at
            """,
            {
                "person_id": str(user_data.person_id),
                "tier": user_data.tier,
                "notes": user_data.notes,
                "updated_at": _dt_to_str(user_data.updated_at),
            },
        )
        self._conn.commit()

    def get_tags_for_person(self, person_id: PersonId) -> list[str]:
        cursor = self._conn.execute(
            "SELECT tag FROM person_tag WHERE person_id = ? ORDER BY tag",
            (str(person_id),),
        )
        return [row["tag"] for row in cursor.fetchall()]

    def add_person_tag(self, person_id: PersonId, tag: str) -> None:
        self._conn.execute(
            "INSERT INTO person_tag (person_id, tag) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (str(person_id), tag),
        )
        self._conn.commit()

    def add_category_feedback(self, feedback: CategoryFeedback) -> None:
        self._conn.execute(
            """
            INSERT INTO category_feedback
                (feedback_id, person_id, signal, note, category, judge_verdict, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(feedback.feedback_id),
                str(feedback.person_id),
                _enum_value(feedback.signal),
                feedback.note,
                feedback.category,
                feedback.judge_verdict,
                _dt_to_str(feedback.created_at),
            ),
        )
        self._conn.commit()

    def get_feedback_for_person(self, person_id: PersonId) -> list[CategoryFeedback]:
        cursor = self._conn.execute(
            "SELECT * FROM category_feedback WHERE person_id = ? ORDER BY created_at",
            (str(person_id),),
        )
        return [CategoryFeedback.model_validate(dict(row)) for row in cursor.fetchall()]


@pure
def _enum_value(value: Any) -> str | None:
    # Enum members are str subclasses whose value equals their name; store that
    # string. None passes through so nullable enum columns stay NULL.
    if value is None:
        return None
    return str(value)


def create_and_initialize_store(database_path: Path) -> SqliteCrmStore:
    with log_span("Initializing CRM store at {}", database_path):
        store = SqliteCrmStore(database_path=database_path)
        store.initialize()
    return store
