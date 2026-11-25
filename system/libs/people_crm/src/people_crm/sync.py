from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Final

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.primitives import NonNegativeInt
from loguru import logger

from people_crm.data_types import (
    EmailIdentity,
    MetricsConfig,
    ObservedName,
    OrgDomainSource,
    ParsedInteraction,
    ParticipantRole,
    SyncResult,
)
from people_crm.errors import GoogleApiError
from people_crm.identity import resolve_orgs, resolve_person_graph
from people_crm.ingest import (
    extract_contact_email_groups,
    parse_calendar_event,
    parse_gmail_message,
)
from people_crm.interfaces import CrmStoreInterface, GoogleApiClientInterface
from people_crm.metrics import compute_person_metrics
from people_crm.primitives import EmailAddress

_THREE_YEARS: Final[timedelta] = timedelta(days=365 * 3)
_DEFAULT_GMAIL_MAX_MESSAGES: Final[int] = 5000


def _upsert_identities_from_parsed(
    store: CrmStoreInterface, parsed: ParsedInteraction
) -> None:
    name_by_email: dict[EmailAddress, ObservedName] = {
        obs.email: obs for obs in parsed.observed_names
    }
    occurred_at = parsed.interaction.occurred_at
    for participant in parsed.interaction.participants:
        # The user themselves is never a CRM person; only build identities for others.
        if participant.role == ParticipantRole.SELF:
            continue
        observed = name_by_email.get(participant.email)
        identity = EmailIdentity(
            email=participant.email,
            domain=participant.email.domain,
            observed_name=observed.name if observed is not None else None,
            observed_name_source=observed.source if observed is not None else None,
            is_free_email_domain=participant.email.is_free_email,
            first_seen_at=occurred_at,
            last_seen_at=occurred_at,
        )
        store.upsert_email_identity(identity)


def ingest_calendar_events(
    store: CrmStoreInterface,
    client: GoogleApiClientInterface,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
    self_emails: Sequence[EmailAddress],
    ingested_at: datetime,
    search_query: str | None,
) -> int:
    """Fetch calendar events (optionally filtered by query) and upsert them into the source layer."""
    with log_span("Ingesting calendar events for {}", calendar_id):
        raw_events = client.fetch_calendar_events(
            calendar_id, time_min, time_max, search_query
        )
    ingested_count = 0
    for raw_event in raw_events:
        parsed = parse_calendar_event(raw_event, self_emails, ingested_at)
        if parsed is None:
            continue
        store.upsert_interaction(parsed.interaction)
        _upsert_identities_from_parsed(store, parsed)
        ingested_count += 1
    logger.debug("Ingested {} calendar interactions", ingested_count)
    return ingested_count


def ingest_gmail_messages(
    store: CrmStoreInterface,
    client: GoogleApiClientInterface,
    message_ids: Sequence[str],
    self_emails: Sequence[EmailAddress],
    ingested_at: datetime,
) -> int:
    """Fetch metadata for the given Gmail message ids and upsert them into the source layer.

    A message that still fails to fetch after the client's own retries is skipped
    (and logged) rather than aborting the whole sync -- one unreachable message out
    of thousands must not cost us the entire mailbox and the downstream recompute.
    """
    ingested_count = 0
    skipped_count = 0
    for message_id in message_ids:
        try:
            raw_message = client.fetch_gmail_message_metadata(message_id)
        except GoogleApiError as error:
            skipped_count += 1
            logger.warning("Skipping Gmail message {}: {}", message_id, error)
            continue
        parsed = parse_gmail_message(raw_message, self_emails, ingested_at)
        if parsed is None:
            continue
        store.upsert_interaction(parsed.interaction)
        _upsert_identities_from_parsed(store, parsed)
        ingested_count += 1
    if skipped_count:
        logger.warning(
            "Skipped {} of {} Gmail messages that could not be fetched",
            skipped_count,
            len(message_ids),
        )
    logger.debug("Ingested {} email interactions", ingested_count)
    return ingested_count


def ingest_mailbox(
    store: CrmStoreInterface,
    client: GoogleApiClientInterface,
    self_emails: Sequence[EmailAddress],
    time_min: datetime,
    ingested_at: datetime,
    max_messages: int,
) -> int:
    """Ingest the user's Gmail messages since time_min, subject to a logged cap."""
    query = f"after:{time_min.strftime('%Y/%m/%d')}"
    with log_span("Fetching Gmail message ids since {}", time_min.date()):
        message_ids = client.fetch_gmail_message_ids(query, max_messages)
    return ingest_gmail_messages(store, client, message_ids, self_emails, ingested_at)


def resolve_and_recompute(
    store: CrmStoreInterface,
    client: GoogleApiClientInterface,
    config: MetricsConfig,
    today: date,
    now: datetime,
) -> SyncResult:
    """Rebuild orgs, the person graph, and per-person metrics from the current source log + user data."""
    identities = store.get_all_email_identities()
    domains = [identity.domain for identity in identities]

    # Preserve seed domain->org mappings across rebuilds by reading them back.
    existing_orgs = store.get_all_orgs()
    existing_org_domains = store.get_all_org_domains()
    org_name_by_id = {org.org_id: org.name for org in existing_orgs}
    seed_domain_to_org_name = {
        org_domain.domain: org_name_by_id[org_domain.org_id]
        for org_domain in existing_org_domains
        if org_domain.source == OrgDomainSource.SEED
        and org_domain.org_id in org_name_by_id
    }
    orgs, org_domains = resolve_orgs(
        domains, seed_domain_to_org_name, existing_orgs, now
    )
    for org in orgs:
        store.upsert_org(org)
    for org_domain in org_domains:
        store.upsert_org_domain(org_domain)
    org_id_by_domain = {
        org_domain.domain: org_domain.org_id for org_domain in org_domains
    }

    contact_groups = extract_contact_email_groups(client.fetch_contacts())
    persons, links = resolve_person_graph(
        identities=identities,
        contact_groups=contact_groups,
        overrides=store.get_all_identity_overrides(),
        existing_persons=store.get_all_persons(),
        existing_links=store.get_all_person_email_links(),
        person_ids_with_user_data=store.get_person_ids_with_user_data(),
        org_id_by_domain=org_id_by_domain,
        now=now,
    )
    store.replace_person_graph(persons, links)

    email_to_person_id = {link.email: link.person_id for link in links}
    interactions = store.get_all_interactions()
    metrics = compute_person_metrics(
        interactions, email_to_person_id, config, today, now
    )
    store.replace_person_metrics(metrics)

    return SyncResult(
        calendar_interactions_ingested=NonNegativeInt(0),
        email_interactions_ingested=NonNegativeInt(0),
        person_count=NonNegativeInt(len(persons)),
        org_count=NonNegativeInt(len(orgs)),
        metric_count=NonNegativeInt(len(metrics)),
    )


def run_full_sync(
    store: CrmStoreInterface,
    client: GoogleApiClientInterface,
    calendar_id: str,
    config: MetricsConfig,
    today: date,
    now: datetime,
    gmail_max_messages: int,
) -> SyncResult:
    """Run a complete idempotent sync: ingest calendar + email, then resolve and recompute."""
    time_min = now - _THREE_YEARS
    calendar_count = ingest_calendar_events(
        store, client, calendar_id, time_min, now, config.self_emails, now, None
    )
    email_count = ingest_mailbox(
        store, client, config.self_emails, time_min, now, gmail_max_messages
    )
    resolved = resolve_and_recompute(store, client, config, today, now)
    return SyncResult(
        calendar_interactions_ingested=NonNegativeInt(calendar_count),
        email_interactions_ingested=NonNegativeInt(email_count),
        person_count=resolved.person_count,
        org_count=resolved.org_count,
        metric_count=resolved.metric_count,
    )
