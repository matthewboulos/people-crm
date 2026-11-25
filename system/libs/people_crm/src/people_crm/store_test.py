from datetime import datetime, timedelta, timezone

from imbue.imbue_common.primitives import NonNegativeInt

from people_crm.data_types import (
    BackfillStatus,
    CategoryFeedback,
    EmailIdentity,
    FeedbackSignal,
    IdentityOverride,
    Interaction,
    InteractionParticipant,
    InteractionType,
    MergeReason,
    NameSource,
    OverrideType,
    ParticipantRole,
    Person,
    PersonEmailLink,
    PersonUserData,
    SourceSystem,
)
from people_crm.primitives import (
    DisplayName,
    EmailAddress,
    FeedbackId,
    InteractionId,
    PersonId,
    SourceRef,
)
from people_crm.store import BUSY_TIMEOUT_SECONDS, SqliteCrmStore

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def _identity(email: str, name: str | None, seen_at: datetime) -> EmailIdentity:
    address = EmailAddress(email)
    return EmailIdentity(
        email=address,
        domain=address.domain,
        observed_name=DisplayName(name) if name else None,
        observed_name_source=NameSource.CALENDAR if name else None,
        is_free_email_domain=address.is_free_email,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )


def _interaction(source_ref: str, email: str) -> Interaction:
    return Interaction(
        interaction_id=InteractionId.generate(),
        interaction_type=InteractionType.CALENDAR,
        source_system=SourceSystem.GOOGLE_CALENDAR,
        source_ref=SourceRef(source_ref),
        source_link="https://calendar.google.com/x",
        occurred_at=_NOW,
        duration_minutes=NonNegativeInt(30),
        title="Sync",
        recurring_series_ref=None,
        direction=None,
        participants=(
            InteractionParticipant(
                email=EmailAddress(email),
                role=ParticipantRole.ATTENDEE,
                response_status=None,
            ),
        ),
        raw_payload="{}",
        ingested_at=_NOW,
    )


def test_email_identity_upsert_widens_window_and_fills_name(
    crm_store: SqliteCrmStore,
) -> None:
    early = _NOW - timedelta(days=10)
    late = _NOW
    crm_store.upsert_email_identity(_identity("a@example.org", None, late))
    crm_store.upsert_email_identity(_identity("a@example.org", "Alice", early))

    stored = crm_store.get_all_email_identities()

    assert len(stored) == 1
    assert stored[0].observed_name == "Alice"
    assert stored[0].first_seen_at == early
    assert stored[0].last_seen_at == late


def test_email_identity_window_widens_by_instant_across_offsets(
    crm_store: SqliteCrmStore,
) -> None:
    # The two later timestamps are the same instant expressed in different
    # offsets; a naive lexicographic MIN()/MAX() over the ISO strings would
    # order them incorrectly. The window must be compared by instant.
    early = datetime(2024, 1, 1, 2, 0, 0, tzinfo=timezone(timedelta(hours=-8)))
    late_utc = datetime(2024, 6, 1, 18, 0, 0, tzinfo=timezone.utc)
    late_pacific = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=-8)))
    crm_store.upsert_email_identity(_identity("a@example.org", None, late_utc))
    crm_store.upsert_email_identity(_identity("a@example.org", None, late_pacific))
    crm_store.upsert_email_identity(_identity("a@example.org", None, early))

    stored = crm_store.get_all_email_identities()

    assert len(stored) == 1
    assert stored[0].first_seen_at == early
    assert stored[0].last_seen_at == late_utc


def test_interaction_upsert_is_idempotent_by_source_ref(
    crm_store: SqliteCrmStore,
) -> None:
    first = _interaction("evt-1", "a@example.org")
    crm_store.upsert_interaction(first)
    duplicate = _interaction("evt-1", "a@example.org")
    crm_store.upsert_interaction(duplicate)

    stored = crm_store.get_all_interactions()

    assert len(stored) == 1
    # The originally stored id is preserved, not the second candidate id.
    assert stored[0].interaction_id == first.interaction_id


def test_replace_person_graph_preserves_user_data(crm_store: SqliteCrmStore) -> None:
    person_id = PersonId.generate()
    person = Person(
        person_id=person_id,
        canonical_email=EmailAddress("a@example.org"),
        display_name=DisplayName("Alice"),
        display_name_source=NameSource.CALENDAR,
        org_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    link = PersonEmailLink(
        email=EmailAddress("a@example.org"),
        person_id=person_id,
        merge_reason=MergeReason.SINGLE_EMAIL,
        merge_confidence=None,
        merged_at=_NOW,
    )
    crm_store.replace_person_graph([person], [link])
    crm_store.upsert_person_user_data(
        PersonUserData(
            person_id=person_id, tier="A", notes="key contact", updated_at=_NOW
        )
    )
    crm_store.add_person_tag(person_id, "investor")

    # Rebuild the derived graph (as a re-sync would).
    crm_store.replace_person_graph([person], [link])

    user_data = crm_store.get_person_user_data(person_id)
    assert user_data is not None
    assert user_data.notes == "key contact"
    assert crm_store.get_tags_for_person(person_id) == ["investor"]
    assert person_id in crm_store.get_person_ids_with_user_data()


def test_identity_override_round_trips_and_status_updates(
    crm_store: SqliteCrmStore,
) -> None:
    override = IdentityOverride(
        email=EmailAddress("new@gmail.com"),
        override_type=OverrideType.ADD_EMAIL_TO_PERSON,
        forced_person_id=PersonId.generate(),
        reason="same person",
        backfill_status=BackfillStatus.PENDING,
        backfilled_at=None,
        created_at=_NOW,
    )
    crm_store.upsert_identity_override(override)
    crm_store.set_override_backfill_status(
        EmailAddress("new@gmail.com"), BackfillStatus.DONE, _NOW
    )

    stored = crm_store.get_all_identity_overrides()

    assert len(stored) == 1
    assert stored[0].backfill_status == BackfillStatus.DONE
    assert stored[0].backfilled_at == _NOW


def test_category_feedback_round_trips(crm_store: SqliteCrmStore) -> None:
    person_id = PersonId.generate()
    feedback = CategoryFeedback(
        feedback_id=FeedbackId.generate(),
        person_id=person_id,
        signal=FeedbackSignal.POSITIVE,
        note="great mentor",
        category="mentor",
        judge_verdict='{"suggested_categories": ["mentor"]}',
        created_at=_NOW,
    )
    crm_store.add_category_feedback(feedback)

    stored = crm_store.get_feedback_for_person(person_id)

    assert len(stored) == 1
    assert stored[0].signal == FeedbackSignal.POSITIVE
    assert stored[0].note == "great mentor"


def test_writes_wait_out_a_slow_reader_instead_of_failing(crm_store: SqliteCrmStore) -> None:
    # The relationships view holds a read lock while it copies the whole store,
    # which can take longer than sqlite's 5-second default wait; a sync that gave
    # up after 5 seconds died with "database is locked" mid-run.
    busy_timeout_ms = crm_store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout_ms == int(BUSY_TIMEOUT_SECONDS * 1000)
    assert BUSY_TIMEOUT_SECONDS >= 60
