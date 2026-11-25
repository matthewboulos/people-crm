from datetime import datetime, timezone

from people_crm.data_types import (
    EmailDirection,
    InteractionType,
    NameSource,
    ParticipantRole,
    ResponseStatus,
)
from people_crm.ingest import (
    infer_name_from_email,
    is_person_email,
    parse_calendar_event,
    parse_gmail_message,
)
from people_crm.primitives import EmailAddress
from people_crm.testing import build_raw_calendar_event, build_raw_gmail_message

_SELF_EMAILS = (EmailAddress("me@example.com"),)
_INGESTED_AT = datetime(2026, 7, 6, tzinfo=timezone.utc)


def test_parse_calendar_event_extracts_participants_and_duration() -> None:
    raw_event = build_raw_calendar_event(
        event_id="evt-1",
        summary="1:1 with Jordan",
        start_datetime="2024-01-01T10:00:00-08:00",
        end_datetime="2024-01-01T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "accepted", None),
            ("jordan.lee@example.net", False, "accepted", "Jordan Lee"),
        ],
        recurring_event_id=None,
        start_date=None,
    )

    parsed = parse_calendar_event(raw_event, _SELF_EMAILS, _INGESTED_AT)

    assert parsed is not None
    assert parsed.interaction.interaction_type == InteractionType.CALENDAR
    assert parsed.interaction.duration_minutes == 60
    assert parsed.interaction.occurred_at.date().isoformat() == "2024-01-01"
    roles_by_email = {str(p.email): p.role for p in parsed.interaction.participants}
    assert roles_by_email["me@example.com"] == ParticipantRole.SELF
    assert roles_by_email["jordan.lee@example.net"] == ParticipantRole.ATTENDEE
    assert parsed.observed_names[0].name == "Jordan Lee"
    assert parsed.observed_names[0].source == NameSource.CALENDAR


def test_parse_calendar_event_marks_all_day_with_null_duration() -> None:
    raw_event = build_raw_calendar_event(
        event_id="evt-2",
        summary="Conference",
        start_datetime=None,
        end_datetime=None,
        attendees=[
            ("me@example.com", True, "accepted", None),
            ("a@x.com", False, "accepted", None),
        ],
        recurring_event_id=None,
        start_date="2024-05-01",
    )

    parsed = parse_calendar_event(raw_event, _SELF_EMAILS, _INGESTED_AT)

    assert parsed is not None
    assert parsed.interaction.duration_minutes is None


def test_parse_calendar_event_returns_none_when_no_other_person() -> None:
    raw_event = build_raw_calendar_event(
        event_id="evt-3",
        summary="Focus time",
        start_datetime="2024-01-01T10:00:00-08:00",
        end_datetime="2024-01-01T11:00:00-08:00",
        attendees=[("me@example.com", True, "accepted", None)],
        recurring_event_id=None,
        start_date=None,
    )

    assert parse_calendar_event(raw_event, _SELF_EMAILS, _INGESTED_AT) is None


def test_parse_calendar_event_captures_declined_self_response() -> None:
    raw_event = build_raw_calendar_event(
        event_id="evt-4",
        summary="Optional sync",
        start_datetime="2024-01-01T10:00:00-08:00",
        end_datetime="2024-01-01T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "declined", None),
            ("b@x.com", False, "accepted", None),
        ],
        recurring_event_id=None,
        start_date=None,
    )

    parsed = parse_calendar_event(raw_event, _SELF_EMAILS, _INGESTED_AT)

    assert parsed is not None
    self_participant = next(
        p for p in parsed.interaction.participants if p.role == ParticipantRole.SELF
    )
    assert self_participant.response_status == ResponseStatus.DECLINED


def test_parse_calendar_event_skips_malformed_attendee_address() -> None:
    # An address with an '@' but a dotless domain passes the resource-address
    # filter yet is not a well-formed EmailAddress; it must be dropped rather
    # than aborting the whole event parse.
    raw_event = build_raw_calendar_event(
        event_id="evt-bad",
        summary="Sync",
        start_datetime="2024-01-01T10:00:00-08:00",
        end_datetime="2024-01-01T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "accepted", None),
            ("noreply@localhost", False, "accepted", None),
            ("jordan.lee@example.net", False, "accepted", None),
        ],
        recurring_event_id=None,
        start_date=None,
    )

    parsed = parse_calendar_event(raw_event, _SELF_EMAILS, _INGESTED_AT)

    assert parsed is not None
    emails = {str(p.email) for p in parsed.interaction.participants}
    assert emails == {"me@example.com", "jordan.lee@example.net"}


def test_parse_gmail_message_skips_malformed_header_address() -> None:
    # A malformed recipient address must be dropped without crashing while the
    # valid sender is still parsed.
    raw_message = build_raw_gmail_message(
        message_id="msg-bad",
        from_header="Jordan Lee <jordan.lee@example.net>",
        to_header="me@example.com, service@localhost",
        cc_header=None,
        subject="Re: coffee",
        date_header="Mon, 01 Jan 2024 10:00:00 -0800",
    )

    parsed = parse_gmail_message(raw_message, _SELF_EMAILS, _INGESTED_AT)

    assert parsed is not None
    emails = {str(p.email) for p in parsed.interaction.participants}
    assert emails == {"me@example.com", "jordan.lee@example.net"}


def test_parse_gmail_message_infers_received_direction() -> None:
    raw_message = build_raw_gmail_message(
        message_id="msg-1",
        from_header="Jordan Lee <jordan.lee@example.net>",
        to_header="me@example.com",
        cc_header=None,
        subject="Re: coffee",
        date_header="Mon, 01 Jan 2024 10:00:00 -0800",
    )

    parsed = parse_gmail_message(raw_message, _SELF_EMAILS, _INGESTED_AT)

    assert parsed is not None
    assert parsed.interaction.direction == EmailDirection.RECEIVED
    assert parsed.interaction.interaction_type == InteractionType.EMAIL
    roles_by_email = {str(p.email): p.role for p in parsed.interaction.participants}
    assert roles_by_email["jordan.lee@example.net"] == ParticipantRole.SENDER
    assert roles_by_email["me@example.com"] == ParticipantRole.SELF


def test_parse_gmail_message_infers_sent_direction() -> None:
    raw_message = build_raw_gmail_message(
        message_id="msg-2",
        from_header="me@example.com",
        to_header="Jordan <jordan.lee@example.net>",
        cc_header=None,
        subject="coffee?",
        date_header="Mon, 01 Jan 2024 10:00:00 -0800",
    )

    parsed = parse_gmail_message(raw_message, _SELF_EMAILS, _INGESTED_AT)

    assert parsed is not None
    assert parsed.interaction.direction == EmailDirection.SENT


def test_infer_name_from_email_handles_dotted_local_part() -> None:
    assert infer_name_from_email("jane.doe@x.com") == ("Jane Doe", NameSource.INFERRED)


def test_infer_name_from_email_rejects_generic_role_addresses() -> None:
    assert infer_name_from_email("info@x.com") is None


def test_is_person_email_excludes_resource_calendars() -> None:
    assert is_person_email("room@resource.calendar.google.com") is False
    assert is_person_email("alice@example.org") is True
