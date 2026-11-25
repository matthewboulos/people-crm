from datetime import date, datetime, timezone

from people_crm.backfill import add_email_to_person_and_backfill
from people_crm.data_types import BackfillStatus, MergeReason
from people_crm.metrics import DEFAULT_METRICS_CONFIG, metrics_config_for_account
from people_crm.mock_google_api_client_test import make_mock_google_api_client
from people_crm.primitives import EmailAddress, PersonId
from people_crm.store import SqliteCrmStore
from people_crm.sync import resolve_and_recompute, run_full_sync
from people_crm.testing import build_raw_calendar_event, build_raw_gmail_message

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 7, 6)
# The connected account is the user, as the CLI derives it at sync time.
_CONFIG = metrics_config_for_account(DEFAULT_METRICS_CONFIG, EmailAddress("me@example.com"))


def _event(event_id: str, day: str, other_email: str, other_name: str) -> dict:
    return build_raw_calendar_event(
        event_id=event_id,
        summary="1:1",
        start_datetime=f"{day}T10:00:00-08:00",
        end_datetime=f"{day}T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "accepted", None),
            (other_email, False, "accepted", other_name),
        ],
        recurring_event_id=None,
        start_date=None,
    )


def _person_id_for(store: SqliteCrmStore, email: str) -> PersonId:
    mapping = {
        str(link.email): link.person_id for link in store.get_all_person_email_links()
    }
    return mapping[email]


def test_add_email_backfills_unseen_history_and_attributes_to_person(
    crm_store: SqliteCrmStore,
) -> None:
    # Initial sync knows Dana only by her work address.
    initial_client = make_mock_google_api_client(
        calendar_events=[_event("e-work", "2024-01-01", "dana@example.org", "Dana Lee")],
        gmail_message_ids=[],
        gmail_messages_by_id={},
        contacts=[],
    )
    run_full_sync(
        crm_store,
        initial_client,
        "me@example.com",
        _CONFIG,
        _TODAY,
        _NOW,
        5000,
    )
    dana_id = _person_id_for(crm_store, "dana@example.org")
    before = next(
        m for m in crm_store.get_all_person_metrics() if m.person_id == dana_id
    )
    assert before.meeting_count == 1

    # A previously unseen personal address, discoverable only by targeted query.
    backfill_client = make_mock_google_api_client(
        calendar_events=[_event("e-home", "2024-03-01", "dana@gmail.com", "Dana Lee")],
        gmail_message_ids=["mm-1"],
        gmail_messages_by_id={
            "mm-1": build_raw_gmail_message(
                message_id="mm-1",
                from_header="Dana <dana@gmail.com>",
                to_header="me@example.com",
                cc_header=None,
                subject="hi",
                date_header="Fri, 01 Mar 2024 09:00:00 -0800",
            )
        },
        contacts=[],
    )

    ingested = add_email_to_person_and_backfill(
        crm_store,
        backfill_client,
        EmailAddress("dana@gmail.com"),
        dana_id,
        "me@example.com",
        _CONFIG,
        _TODAY,
        _NOW,
        "same person, personal email",
        5000,
    )

    assert ingested == 2
    # The added email now belongs to Dana, by a manual (auditable) link.
    assert _person_id_for(crm_store, "dana@gmail.com") == dana_id
    manual_link = next(
        link
        for link in crm_store.get_all_person_email_links()
        if str(link.email) == "dana@gmail.com"
    )
    assert manual_link.merge_reason == MergeReason.MANUAL
    # Her metrics now include the backfilled meeting.
    after = next(
        m for m in crm_store.get_all_person_metrics() if m.person_id == dana_id
    )
    assert after.meeting_count == 2
    override = next(
        o
        for o in crm_store.get_all_identity_overrides()
        if str(o.email) == "dana@gmail.com"
    )
    assert override.backfill_status == BackfillStatus.DONE


def test_added_email_survives_a_later_resync(crm_store: SqliteCrmStore) -> None:
    initial_client = make_mock_google_api_client(
        calendar_events=[_event("e-work", "2024-01-01", "dana@example.org", "Dana Lee")],
        gmail_message_ids=[],
        gmail_messages_by_id={},
        contacts=[],
    )
    run_full_sync(
        crm_store,
        initial_client,
        "me@example.com",
        _CONFIG,
        _TODAY,
        _NOW,
        5000,
    )
    dana_id = _person_id_for(crm_store, "dana@example.org")

    backfill_client = make_mock_google_api_client(
        calendar_events=[_event("e-home", "2024-03-01", "dana@gmail.com", "Dana Lee")],
        gmail_message_ids=[],
        gmail_messages_by_id={},
        contacts=[],
    )
    add_email_to_person_and_backfill(
        crm_store,
        backfill_client,
        EmailAddress("dana@gmail.com"),
        dana_id,
        "me@example.com",
        _CONFIG,
        _TODAY,
        _NOW,
        None,
        5000,
    )

    # A later resolution pass (as a routine re-sync would run) must not drop the
    # manual mapping, even though this client never re-surfaces the home event.
    resolve_and_recompute(
        crm_store, initial_client, _CONFIG, _TODAY, _NOW
    )

    assert _person_id_for(crm_store, "dana@gmail.com") == dana_id
