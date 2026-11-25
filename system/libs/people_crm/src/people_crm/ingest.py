import email.utils
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Final

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.pure import pure

from people_crm.data_types import (
    EmailDirection,
    Interaction,
    InteractionParticipant,
    InteractionType,
    NameSource,
    ObservedName,
    ParsedInteraction,
    ParticipantRole,
    ResponseStatus,
    SourceSystem,
)
from people_crm.primitives import (
    DisplayName,
    EmailAddress,
    InteractionId,
    InvalidEmailAddressError,
    SourceRef,
)

# Title keywords that mark a group social/company event rather than a working
# meeting (only applied when >2 people are present). Preserved verbatim from the
# meeting-contacts prototype so metric definitions stay identical.
SOCIAL_TITLE_KEYWORDS: Final[tuple[str, ...]] = (
    "dinner",
    "lunch",
    "breakfast",
    "brunch",
    " coffee",
    "koffee",
    "drinks",
    "happy hour",
    "party",
    "celebration",
    "holiday",
    "offsite",
    "off-site",
    "onsite",
    "on-site",
    "retreat",
    "festival",
    "+1",
    "all hands",
    "all-hands",
    "allhands",
    " social",
    "outing",
    "welcome",
    "farewell",
    "birthday",
    "game night",
    "mixer",
    "reception",
    "gathering",
    "team event",
    "team dinner",
    "team lunch",
    " hike",
    " tea ",
    "xmas",
    "christmas",
    "happy-hour",
    "get together",
    "get-together",
    "team building",
)

# Local-parts that are role addresses, not people; never inferred into a name.
_GENERIC_LOCAL_PARTS: Final[frozenset[str]] = frozenset(
    {
        "info",
        "team",
        "hello",
        "contact",
        "admin",
        "support",
        "sales",
        "press",
        "office",
        "events",
        "hi",
        "no-reply",
        "noreply",
        "help",
        "general",
        "inquiries",
        "media",
        "donotreply",
    }
)

_RESPONSE_STATUS_BY_GOOGLE_VALUE: Final[Mapping[str, ResponseStatus]] = {
    "accepted": ResponseStatus.ACCEPTED,
    "declined": ResponseStatus.DECLINED,
    "tentative": ResponseStatus.TENTATIVE,
    "needsAction": ResponseStatus.NEEDS_ACTION,
}


@pure
def is_person_email(email_value: str) -> bool:
    # Calendar attendee lists include room/resource pseudo-addresses; exclude them.
    if not email_value or "@" not in email_value:
        return False
    domain = email_value.rsplit("@", 1)[-1]
    return not (
        domain.endswith("group.calendar.google.com")
        or domain.endswith("resource.calendar.google.com")
    )


@pure
def _parse_person_email(email_value: str) -> EmailAddress | None:
    # A single choke point for turning a raw address into a validated
    # EmailAddress. is_person_email only rejects resource/room pseudo-addresses;
    # a plausible address can still be malformed (e.g. a domain with no dot), in
    # which case we drop that participant rather than aborting the whole ingest.
    if not is_person_email(email_value):
        return None
    try:
        return EmailAddress(email_value)
    except InvalidEmailAddressError:
        return None


@pure
def infer_name_from_email(email_value: str) -> tuple[str, NameSource] | None:
    # Best-effort name inference from the local-part (e.g. "jane.doe" -> "Jane Doe").
    local_part = email_value.split("@")[0]
    if local_part.lower() in _GENERIC_LOCAL_PARTS:
        return None
    tokens = [re.sub(r"\d+", "", part) for part in re.split(r"[._-]+", local_part)]
    tokens = [token for token in tokens if len(token) >= 2]
    if len(tokens) >= 2:
        return " ".join(token.capitalize() for token in tokens), NameSource.INFERRED
    if len(tokens) == 1 and tokens[0].isalpha() and 3 <= len(tokens[0]) <= 7:
        return tokens[0].capitalize(), NameSource.INFERRED_WEAK
    return None


@pure
def _parse_google_datetime(value: str) -> datetime:
    # Preserve the event's original UTC offset (still timezone-aware). The wall
    # date in the user's own timezone is what "the day we met" means, and keeping
    # the offset reproduces the calendar's local meeting dates exactly. A naive
    # value (should not occur for Google times) is treated as UTC.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@pure
def _clean_observed_name(raw_name: str | None, email_value: str) -> str | None:
    if not raw_name:
        return None
    cleaned = raw_name.strip().strip("'\"")
    if not cleaned or cleaned.lower() == email_value.strip().lower():
        return None
    return cleaned


@pure
def parse_calendar_event(
    raw_event: Mapping[str, Any],
    self_emails: Sequence[EmailAddress],
    ingested_at: datetime,
) -> ParsedInteraction | None:
    """Parse one raw Google Calendar event into an interaction, or None if it has no other person."""
    start = raw_event.get("start", {})
    end = raw_event.get("end", {})
    self_email_set = {str(e) for e in self_emails}

    # Timed events carry dateTime; all-day events carry only a date. All-day
    # events are preserved but given a null duration so metrics exclude them.
    if "dateTime" in start:
        occurred_at = _parse_google_datetime(start["dateTime"])
        end_at = _parse_google_datetime(end["dateTime"]) if "dateTime" in end else None
        raw_duration = (
            int((end_at - occurred_at).total_seconds() // 60)
            if end_at is not None
            else 0
        )
        duration_minutes: NonNegativeInt | None = NonNegativeInt(max(0, raw_duration))
    elif "date" in start:
        occurred_at = _parse_google_datetime(start["date"] + "T00:00:00+00:00")
        duration_minutes = None
    else:
        return None

    participants: list[InteractionParticipant] = []
    observed_names: list[ObservedName] = []
    has_other_person = False
    for attendee in raw_event.get("attendees", []):
        email_value = (attendee.get("email") or "").lower()
        if not email_value or attendee.get("resource"):
            continue
        address = _parse_person_email(email_value)
        if address is None:
            continue
        is_self = bool(attendee.get("self")) or email_value in self_email_set
        role = ParticipantRole.SELF if is_self else ParticipantRole.ATTENDEE
        response_status = _RESPONSE_STATUS_BY_GOOGLE_VALUE.get(
            attendee.get("responseStatus", "")
        )
        participants.append(
            InteractionParticipant(
                email=address, role=role, response_status=response_status
            )
        )
        if not is_self:
            has_other_person = True
        cleaned_name = _clean_observed_name(attendee.get("displayName"), email_value)
        if cleaned_name is not None:
            observed_names.append(
                ObservedName(
                    email=address,
                    name=DisplayName(cleaned_name),
                    source=NameSource.CALENDAR,
                )
            )

    if not has_other_person:
        return None

    interaction = Interaction(
        interaction_id=InteractionId.generate(),
        interaction_type=InteractionType.CALENDAR,
        source_system=SourceSystem.GOOGLE_CALENDAR,
        source_ref=SourceRef(raw_event["id"]),
        source_link=raw_event.get("htmlLink"),
        occurred_at=occurred_at,
        duration_minutes=duration_minutes,
        title=raw_event.get("summary") or "(untitled)",
        recurring_series_ref=raw_event.get("recurringEventId"),
        direction=None,
        participants=tuple(participants),
        raw_payload=json.dumps(raw_event, default=str),
        ingested_at=ingested_at,
    )
    return ParsedInteraction(
        interaction=interaction, observed_names=tuple(observed_names)
    )


@pure
def _header_value_by_name(headers: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {
        header.get("name", "").lower(): header.get("value", "") for header in headers
    }


@pure
def parse_gmail_message(
    raw_message: Mapping[str, Any],
    self_emails: Sequence[EmailAddress],
    ingested_at: datetime,
) -> ParsedInteraction | None:
    """Parse one raw Gmail metadata message into an interaction, or None if it has no other person."""
    payload = raw_message.get("payload", {})
    headers = _header_value_by_name(payload.get("headers", []))
    self_email_set = {str(e) for e in self_emails}

    date_header = headers.get("date")
    if not date_header:
        return None
    try:
        occurred_at = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    from_pairs = email.utils.getaddresses([headers.get("from", "")])
    is_sent_by_user = any(
        address.lower() in self_email_set for _name, address in from_pairs
    )
    direction = EmailDirection.SENT if is_sent_by_user else EmailDirection.RECEIVED

    participants: list[InteractionParticipant] = []
    observed_names: list[ObservedName] = []
    has_other_person = False
    seen: set[tuple[str, ParticipantRole]] = set()

    def _add(role: ParticipantRole, name_value: str, address_value: str) -> None:
        nonlocal has_other_person
        email_value = address_value.lower()
        address = _parse_person_email(email_value)
        if address is None:
            return
        is_self = email_value in self_email_set
        effective_role = ParticipantRole.SELF if is_self else role
        key = (email_value, effective_role)
        if key in seen:
            return
        seen.add(key)
        participants.append(
            InteractionParticipant(
                email=address, role=effective_role, response_status=None
            )
        )
        if not is_self:
            has_other_person = True
        cleaned_name = _clean_observed_name(name_value, email_value)
        if cleaned_name is not None:
            observed_names.append(
                ObservedName(
                    email=address,
                    name=DisplayName(cleaned_name),
                    source=NameSource.GMAIL,
                )
            )

    for name_value, address_value in from_pairs:
        _add(ParticipantRole.SENDER, name_value, address_value)
    for name_value, address_value in email.utils.getaddresses([headers.get("to", "")]):
        _add(ParticipantRole.RECIPIENT_TO, name_value, address_value)
    for name_value, address_value in email.utils.getaddresses([headers.get("cc", "")]):
        _add(ParticipantRole.RECIPIENT_CC, name_value, address_value)

    if not has_other_person:
        return None

    message_id = raw_message["id"]
    interaction = Interaction(
        interaction_id=InteractionId.generate(),
        interaction_type=InteractionType.EMAIL,
        source_system=SourceSystem.GMAIL,
        source_ref=SourceRef(message_id),
        source_link=f"https://mail.google.com/mail/u/0/#all/{message_id}",
        occurred_at=occurred_at,
        duration_minutes=None,
        title=headers.get("subject") or "(no subject)",
        recurring_series_ref=None,
        direction=direction,
        participants=tuple(participants),
        raw_payload=json.dumps(raw_message, default=str),
        ingested_at=ingested_at,
    )
    return ParsedInteraction(
        interaction=interaction, observed_names=tuple(observed_names)
    )


@pure
def extract_contact_email_groups(
    raw_contacts: Sequence[Mapping[str, Any]],
) -> list[list[EmailAddress]]:
    """Group email addresses that share a single Google contact entry (a strong merge signal)."""
    groups: list[list[EmailAddress]] = []
    for connection in raw_contacts:
        addresses: list[EmailAddress] = []
        for entry in connection.get("emailAddresses", []):
            value = (entry.get("value") or "").strip().lower()
            address = _parse_person_email(value) if value else None
            if address is not None:
                addresses.append(address)
        if len(addresses) >= 2:
            groups.append(addresses)
    return groups
