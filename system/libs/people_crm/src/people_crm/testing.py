from collections.abc import Sequence
from typing import Any

from people_crm.interfaces import RawJson


def build_raw_calendar_event(
    event_id: str,
    summary: str,
    start_datetime: str | None,
    end_datetime: str | None,
    attendees: Sequence[tuple[str, bool, str | None, str | None]],
    recurring_event_id: str | None,
    start_date: str | None,
) -> RawJson:
    """Build a raw Google Calendar event dict; attendees are (email, is_self, response, display_name)."""
    raw_attendees: list[dict[str, Any]] = []
    for email, is_self, response, display_name in attendees:
        attendee: dict[str, Any] = {"email": email}
        if is_self:
            attendee["self"] = True
        if response is not None:
            attendee["responseStatus"] = response
        if display_name is not None:
            attendee["displayName"] = display_name
        raw_attendees.append(attendee)

    event: RawJson = {
        "id": event_id,
        "summary": summary,
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        "attendees": raw_attendees,
    }
    if start_datetime is not None and end_datetime is not None:
        event["start"] = {"dateTime": start_datetime}
        event["end"] = {"dateTime": end_datetime}
    elif start_date is not None:
        event["start"] = {"date": start_date}
        event["end"] = {"date": start_date}
    if recurring_event_id is not None:
        event["recurringEventId"] = recurring_event_id
    return event


def build_raw_gmail_message(
    message_id: str,
    from_header: str,
    to_header: str,
    cc_header: str | None,
    subject: str,
    date_header: str,
) -> RawJson:
    """Build a raw Gmail metadata-format message dict."""
    headers: list[dict[str, str]] = [
        {"name": "From", "value": from_header},
        {"name": "To", "value": to_header},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": date_header},
        {"name": "Message-Id", "value": f"<{message_id}@mail.example.com>"},
    ]
    if cc_header is not None:
        headers.append({"name": "Cc", "value": cc_header})
    return {"id": message_id, "payload": {"headers": headers}}


def build_raw_contact(
    email_addresses: Sequence[str], display_name: str | None
) -> RawJson:
    """Build a raw People API connection dict with the given email addresses."""
    connection: RawJson = {
        "emailAddresses": [{"value": address} for address in email_addresses]
    }
    if display_name is not None:
        connection["names"] = [{"displayName": display_name}]
    return connection
