from datetime import date, datetime, timezone

from people_crm.metrics import DEFAULT_METRICS_CONFIG, metrics_config_for_account
from people_crm.mock_google_api_client_test import (
    MockGoogleApiClient,
    make_mock_google_api_client,
)
from people_crm.primitives import EmailAddress, PersonId
from people_crm.store import SqliteCrmStore
from people_crm.sync import run_full_sync
from people_crm.testing import (
    build_raw_calendar_event,
    build_raw_contact,
    build_raw_gmail_message,
)

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 7, 6)
# The connected account is the user, as the CLI derives it at sync time.
_CONFIG = metrics_config_for_account(DEFAULT_METRICS_CONFIG, EmailAddress("me@example.com"))


def _one_on_one_event(event_id: str, day: str) -> dict:
    return build_raw_calendar_event(
        event_id=event_id,
        summary="1:1 with Jordan",
        start_datetime=f"{day}T10:00:00-08:00",
        end_datetime=f"{day}T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "accepted", None),
            ("jordan.lee@example.net", False, "accepted", "Jordan Lee"),
        ],
        recurring_event_id=None,
        start_date=None,
    )


def _email_from_jordan() -> dict:
    return build_raw_gmail_message(
        message_id="m-1",
        from_header="Jordan Lee <jordan.lee@example.net>",
        to_header="me@example.com",
        cc_header=None,
        subject="thanks",
        date_header="Mon, 08 Jan 2024 09:00:00 -0800",
    )


def _person_id_for(store: SqliteCrmStore, email: str) -> str:
    mapping = {
        str(link.email): link.person_id for link in store.get_all_person_email_links()
    }
    return str(mapping[email])


def test_full_sync_ingests_calendar_and_email_and_builds_metrics(
    crm_store: SqliteCrmStore,
) -> None:
    client = make_mock_google_api_client(
        calendar_events=[_one_on_one_event("e-1", "2024-01-01")],
        gmail_message_ids=["m-1"],
        gmail_messages_by_id={"m-1": _email_from_jordan()},
        contacts=[],
    )

    result = run_full_sync(
        crm_store, client, "primary", _CONFIG, _TODAY, _NOW, 5000
    )

    assert result.calendar_interactions_ingested == 1
    assert result.email_interactions_ingested == 1
    assert result.person_count == 1
    person_id = _person_id_for(crm_store, "jordan.lee@example.net")
    metric = next(
        m for m in crm_store.get_all_person_metrics() if str(m.person_id) == person_id
    )
    assert metric.meeting_count == 1
    assert metric.one_on_one_count == 1
    # Both a calendar and an email interaction are persisted in the source log.
    assert len(crm_store.get_all_interactions()) == 2


def test_repeated_sync_is_idempotent_and_preserves_user_data(
    crm_store: SqliteCrmStore,
) -> None:
    client = make_mock_google_api_client(
        calendar_events=[_one_on_one_event("e-1", "2024-01-01")],
        gmail_message_ids=["m-1"],
        gmail_messages_by_id={"m-1": _email_from_jordan()},
        contacts=[],
    )
    run_full_sync(
        crm_store, client, "primary", _CONFIG, _TODAY, _NOW, 5000
    )

    person_id = PersonId(_person_id_for(crm_store, "jordan.lee@example.net"))
    crm_store.add_person_tag(person_id, "investor")

    # Re-run the whole sync; nothing should duplicate and the tag must survive.
    run_full_sync(
        crm_store, client, "primary", _CONFIG, _TODAY, _NOW, 5000
    )

    assert len(crm_store.get_all_interactions()) == 2
    assert crm_store.get_all_persons()[0].person_id == person_id
    assert crm_store.get_tags_for_person(person_id) == ["investor"]


def test_sync_skips_unfetchable_gmail_message_and_still_completes(
    crm_store: SqliteCrmStore,
) -> None:
    # One Gmail message fetches fine; a second persistently fails. The failing one
    # must be skipped (not abort the run), and calendar ingestion + recompute must
    # still complete so metrics are built.
    client = MockGoogleApiClient(
        calendar_events=(_one_on_one_event("e-1", "2024-01-01"),),
        gmail_message_ids=("m-1", "m-bad"),
        gmail_messages_by_id={"m-1": _email_from_jordan()},
        failing_gmail_message_ids=frozenset({"m-bad"}),
        contacts=(),
    )

    result = run_full_sync(
        crm_store, client, "primary", _CONFIG, _TODAY, _NOW, 5000
    )

    # Only the fetchable email was ingested; the sync did not abort.
    assert result.email_interactions_ingested == 1
    assert result.calendar_interactions_ingested == 1
    assert result.person_count == 1
    assert result.metric_count == 1
    # 1 calendar + 1 email interaction; the failed message left no partial row.
    assert len(crm_store.get_all_interactions()) == 2


def test_sync_merges_contacts_into_one_person(crm_store: SqliteCrmStore) -> None:
    work_event = build_raw_calendar_event(
        event_id="e-work",
        summary="1:1",
        start_datetime="2024-01-01T10:00:00-08:00",
        end_datetime="2024-01-01T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "accepted", None),
            ("dana@example.org", False, "accepted", "Dana Lee"),
        ],
        recurring_event_id=None,
        start_date=None,
    )
    home_event = build_raw_calendar_event(
        event_id="e-home",
        summary="1:1",
        start_datetime="2024-02-01T10:00:00-08:00",
        end_datetime="2024-02-01T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "accepted", None),
            ("dana@gmail.com", False, "accepted", "Dana Lee"),
        ],
        recurring_event_id=None,
        start_date=None,
    )
    client = make_mock_google_api_client(
        calendar_events=[work_event, home_event],
        gmail_message_ids=[],
        gmail_messages_by_id={},
        contacts=[build_raw_contact(["dana@example.org", "dana@gmail.com"], "Dana Lee")],
    )

    result = run_full_sync(
        crm_store, client, "primary", _CONFIG, _TODAY, _NOW, 5000
    )

    assert result.person_count == 1
    assert _person_id_for(crm_store, "dana@example.org") == _person_id_for(
        crm_store, "dana@gmail.com"
    )
