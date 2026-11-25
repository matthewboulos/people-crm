from datetime import date, datetime, timedelta
from typing import Final

from imbue.imbue_common.logging import log_span
from loguru import logger

from people_crm.data_types import (
    BackfillStatus,
    IdentityOverride,
    MetricsConfig,
    OverrideType,
)
from people_crm.errors import GoogleApiError, PersonNotFoundError
from people_crm.interfaces import CrmStoreInterface, GoogleApiClientInterface
from people_crm.primitives import EmailAddress, PersonId
from people_crm.sync import (
    ingest_calendar_events,
    ingest_gmail_messages,
    resolve_and_recompute,
)

_THREE_YEARS: Final[timedelta] = timedelta(days=365 * 3)


def add_email_to_person(
    store: CrmStoreInterface,
    email: EmailAddress,
    person_id: PersonId,
    reason: str | None,
    now: datetime,
) -> None:
    """Record a manual override attaching an email to an existing person (queued for backfill)."""
    existing_person_ids = {person.person_id for person in store.get_all_persons()}
    if person_id not in existing_person_ids:
        raise PersonNotFoundError(str(person_id))
    override = IdentityOverride(
        email=email,
        override_type=OverrideType.ADD_EMAIL_TO_PERSON,
        forced_person_id=person_id,
        reason=reason,
        backfill_status=BackfillStatus.PENDING,
        backfilled_at=None,
        created_at=now,
    )
    store.upsert_identity_override(override)


def backfill_email(
    store: CrmStoreInterface,
    client: GoogleApiClientInterface,
    email: EmailAddress,
    calendar_id: str,
    config: MetricsConfig,
    today: date,
    now: datetime,
    gmail_max_messages: int,
) -> int:
    """Fetch a single address's full Google history on demand and attribute it, even if never seen before."""
    time_min = now - _THREE_YEARS
    try:
        with log_span("Backfilling Google history for {}", email):
            calendar_count = ingest_calendar_events(
                store,
                client,
                calendar_id,
                time_min,
                now,
                config.self_emails,
                now,
                str(email),
            )
            message_ids = client.fetch_gmail_message_ids(
                f"from:{email} OR to:{email}", gmail_max_messages
            )
            email_count = ingest_gmail_messages(
                store, client, message_ids, config.self_emails, now
            )
    except GoogleApiError:
        store.set_override_backfill_status(email, BackfillStatus.FAILED, None)
        logger.opt(exception=True).error(
            "Failed to backfill Google history for {}", email
        )
        raise
    resolve_and_recompute(store, client, config, today, now)
    store.set_override_backfill_status(email, BackfillStatus.DONE, now)
    return calendar_count + email_count


def add_email_to_person_and_backfill(
    store: CrmStoreInterface,
    client: GoogleApiClientInterface,
    email: EmailAddress,
    person_id: PersonId,
    calendar_id: str,
    config: MetricsConfig,
    today: date,
    now: datetime,
    reason: str | None,
    gmail_max_messages: int,
) -> int:
    """Attach an email to a person and immediately backfill and attribute its history."""
    add_email_to_person(store, email, person_id, reason, now)
    return backfill_email(
        store, client, email, calendar_id, config, today, now, gmail_max_messages
    )
