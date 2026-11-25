from collections.abc import Sequence
from datetime import date, datetime, timezone

from people_crm.data_types import Interaction
from people_crm.ingest import parse_calendar_event
from people_crm.metrics import (
    DEFAULT_METRICS_CONFIG,
    compute_person_metrics,
    is_social_event,
    metrics_config_for_account,
)
from people_crm.primitives import EmailAddress, PersonId
from people_crm.testing import build_raw_calendar_event

_SELF_EMAILS = (EmailAddress("me@example.com"),)
_INGESTED_AT = datetime(2026, 7, 6, tzinfo=timezone.utc)
_TODAY = date(2026, 7, 6)


def _event(
    event_id: str,
    summary: str,
    day: str,
    other_emails: Sequence[str],
    recurring_event_id: str | None,
    duration_hours: int,
) -> Interaction:
    attendees = [("me@example.com", True, "accepted", None)]
    for email in other_emails:
        attendees.append((email, False, "accepted", None))
    raw = build_raw_calendar_event(
        event_id=event_id,
        summary=summary,
        start_datetime=f"{day}T10:00:00-08:00",
        end_datetime=f"{day}T{10 + duration_hours:02d}:00:00-08:00",
        attendees=attendees,
        recurring_event_id=recurring_event_id,
        start_date=None,
    )
    parsed = parse_calendar_event(raw, _SELF_EMAILS, _INGESTED_AT)
    assert parsed is not None
    return parsed.interaction


def _person_map(emails: Sequence[str]) -> dict[EmailAddress, PersonId]:
    return {EmailAddress(email): PersonId.generate() for email in emails}


def test_recurring_one_on_one_counts_every_occurrence() -> None:
    other = "jordan.lee@example.net"
    interactions = [
        _event("e1", "Weekly 1:1", "2024-01-01", [other], "series-a", 1),
        _event("e2", "Weekly 1:1", "2024-01-08", [other], "series-a", 1),
    ]
    email_to_person = _person_map([other])

    metrics = compute_person_metrics(
        interactions, email_to_person, DEFAULT_METRICS_CONFIG, _TODAY, _INGESTED_AT
    )

    assert len(metrics) == 1
    assert metrics[0].meeting_count == 2
    assert metrics[0].one_on_one_count == 2
    assert metrics[0].focused_count == 2
    assert metrics[0].minutes_total == 120


def test_recurring_group_meeting_collapses_to_one_per_person() -> None:
    others = ["a@team.com", "b@team.com"]
    interactions = [
        _event("g1", "Weekly Sync", "2024-01-01", others, "series-g", 1),
        _event("g2", "Weekly Sync", "2024-01-08", others, "series-g", 1),
        _event("g3", "Weekly Sync", "2024-01-15", others, "series-g", 1),
    ]
    email_to_person = _person_map(others)

    metrics = compute_person_metrics(
        interactions, email_to_person, DEFAULT_METRICS_CONFIG, _TODAY, _INGESTED_AT
    )

    assert {int(m.meeting_count) for m in metrics} == {1}
    # 3 attendees total <= 4 and not social -> focused.
    assert {int(m.focused_count) for m in metrics} == {1}
    assert {int(m.one_on_one_count) for m in metrics} == {0}


def test_social_group_event_is_not_focused() -> None:
    others = ["a@team.com", "b@team.com", "c@team.com"]
    interactions = [_event("s1", "Team Dinner", "2024-02-01", others, None, 2)]
    email_to_person = _person_map(others)

    metrics = compute_person_metrics(
        interactions, email_to_person, DEFAULT_METRICS_CONFIG, _TODAY, _INGESTED_AT
    )

    assert {int(m.meeting_count) for m in metrics} == {1}
    assert {int(m.focused_count) for m in metrics} == {0}
    assert {m.is_group_only for m in metrics} == {True}


def test_declined_event_is_excluded() -> None:
    raw = build_raw_calendar_event(
        event_id="d1",
        summary="Optional",
        start_datetime="2024-03-01T10:00:00-08:00",
        end_datetime="2024-03-01T11:00:00-08:00",
        attendees=[
            ("me@example.com", True, "declined", None),
            ("z@team.com", False, "accepted", None),
        ],
        recurring_event_id=None,
        start_date=None,
    )
    parsed = parse_calendar_event(raw, _SELF_EMAILS, _INGESTED_AT)
    assert parsed is not None
    email_to_person = _person_map(["z@team.com"])

    metrics = compute_person_metrics(
        [parsed.interaction],
        email_to_person,
        DEFAULT_METRICS_CONFIG,
        _TODAY,
        _INGESTED_AT,
    )

    assert metrics == []


def test_all_day_event_is_excluded_from_metrics() -> None:
    raw = build_raw_calendar_event(
        event_id="ad1",
        summary="Conference",
        start_datetime=None,
        end_datetime=None,
        attendees=[
            ("me@example.com", True, "accepted", None),
            ("q@team.com", False, "accepted", None),
        ],
        recurring_event_id=None,
        start_date="2024-04-01",
    )
    parsed = parse_calendar_event(raw, _SELF_EMAILS, _INGESTED_AT)
    assert parsed is not None
    email_to_person = _person_map(["q@team.com"])

    metrics = compute_person_metrics(
        [parsed.interaction],
        email_to_person,
        DEFAULT_METRICS_CONFIG,
        _TODAY,
        _INGESTED_AT,
    )

    assert metrics == []


def test_days_since_last_and_dates_are_computed() -> None:
    other = "jordan.lee@example.net"
    interactions = [
        _event("e1", "First", "2024-01-01", [other], None, 1),
        _event("e2", "Last", "2024-01-31", [other], None, 1),
    ]
    email_to_person = _person_map([other])

    metrics = compute_person_metrics(
        interactions, email_to_person, DEFAULT_METRICS_CONFIG, _TODAY, _INGESTED_AT
    )

    assert metrics[0].first_interaction_date == date(2024, 1, 1)
    assert metrics[0].last_interaction_date == date(2024, 1, 31)
    assert metrics[0].days_since_last == (_TODAY - date(2024, 1, 31)).days


def test_merged_person_counts_a_shared_event_once() -> None:
    # Two emails for the same person attending one meeting should count as a
    # single 1:1, not a group.
    person_id = PersonId.generate()
    email_to_person = {
        EmailAddress("work@example.org"): person_id,
        EmailAddress("home@gmail.com"): person_id,
    }
    interactions = [
        _event(
            "m1", "Sync", "2024-01-01", ["work@example.org", "home@gmail.com"], None, 1
        )
    ]

    metrics = compute_person_metrics(
        interactions, email_to_person, DEFAULT_METRICS_CONFIG, _TODAY, _INGESTED_AT
    )

    assert len(metrics) == 1
    assert metrics[0].one_on_one_count == 1


def test_is_social_event_ignores_one_on_one() -> None:
    assert (
        is_social_event("Coffee", 2, DEFAULT_METRICS_CONFIG.social_title_keywords)
        is False
    )
    assert (
        is_social_event("Team Dinner", 4, DEFAULT_METRICS_CONFIG.social_title_keywords)
        is True
    )


def test_account_email_is_added_to_self_emails_once() -> None:
    account = EmailAddress("me@example.com")
    config = metrics_config_for_account(DEFAULT_METRICS_CONFIG, account)

    assert account in config.self_emails
    assert metrics_config_for_account(config, account).self_emails == config.self_emails
