import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import click
from imbue.imbue_common.logging import setup_logging
from loguru import logger

from people_crm.backfill import add_email_to_person, add_email_to_person_and_backfill
from people_crm.data_types import MetricsConfig
from people_crm.google_api import LatchkeyGoogleApiClient
from people_crm.interfaces import GoogleApiClientInterface
from people_crm.metrics import DEFAULT_METRICS_CONFIG, metrics_config_for_account
from people_crm.migration import migrate_legacy_data
from people_crm.primitives import EmailAddress, PersonId
from people_crm.store import create_and_initialize_store
from people_crm.sync import resolve_and_recompute, run_full_sync
from people_crm.web_view import build_web_view_payload

_DEFAULT_DB_PATH: Final[str] = "runtime/people-crm/crm.db"
# Google's alias for the connected account's own calendar.
_DEFAULT_CALENDAR_ID: Final[str] = "primary"
_DEFAULT_LEGACY_DIR: Final[str] = "runtime/fetch-process-show/meeting-contacts"
_DEFAULT_GMAIL_MAX_MESSAGES: Final[int] = 5000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _account_metrics_config(client: GoogleApiClientInterface) -> MetricsConfig:
    # The user is whoever owns the connected Google account, so their own
    # address is read from it rather than configured.
    return metrics_config_for_account(
        DEFAULT_METRICS_CONFIG, EmailAddress(client.fetch_account_email())
    )


@click.group()
def main() -> None:
    """Manage the durable people-CRM data store."""
    setup_logging(level="INFO")


@main.command(name="migrate")
@click.option(
    "--db", "db_path", default=_DEFAULT_DB_PATH, help="Path to the SQLite store"
)
@click.option(
    "--legacy-dir",
    default=_DEFAULT_LEGACY_DIR,
    help="Directory holding all_contacts.json + filters.json",
)
def migrate_command(db_path: str, legacy_dir: str) -> None:
    """Seed names, filters, and org seeds from the existing meeting-contacts JSON."""
    store = create_and_initialize_store(Path(db_path))
    result = migrate_legacy_data(
        store,
        Path(legacy_dir) / "all_contacts.json",
        Path(legacy_dir) / "filters.json",
        _now(),
    )
    logger.info(
        "Migrated: {} identities, {} filter rules, {} orgs, {} org-domains",
        result.email_identities_seeded,
        result.filter_rules_seeded,
        result.orgs_seeded,
        result.org_domains_seeded,
    )
    store.close()


@main.command(name="sync")
@click.option(
    "--db", "db_path", default=_DEFAULT_DB_PATH, help="Path to the SQLite store"
)
@click.option(
    "--calendar",
    "calendar_id",
    default=_DEFAULT_CALENDAR_ID,
    help="Calendar id to ingest",
)
@click.option(
    "--gmail-max",
    default=_DEFAULT_GMAIL_MAX_MESSAGES,
    help="Max Gmail messages to ingest",
)
def sync_command(db_path: str, calendar_id: str, gmail_max: int) -> None:
    """Ingest calendar + email, then rebuild identities, orgs, and metrics."""
    store = create_and_initialize_store(Path(db_path))
    client = LatchkeyGoogleApiClient()
    now = _now()
    result = run_full_sync(
        store, client, calendar_id, _account_metrics_config(client), now.date(), now, gmail_max
    )
    logger.info(
        "Synced: {} calendar + {} email interactions; {} persons, {} orgs, {} metrics",
        result.calendar_interactions_ingested,
        result.email_interactions_ingested,
        result.person_count,
        result.org_count,
        result.metric_count,
    )
    store.close()


@main.command(name="recompute")
@click.option(
    "--db", "db_path", default=_DEFAULT_DB_PATH, help="Path to the SQLite store"
)
def recompute_command(db_path: str) -> None:
    """Rebuild identities, orgs, and metrics from the existing source log (no new fetch)."""
    store = create_and_initialize_store(Path(db_path))
    client = LatchkeyGoogleApiClient()
    now = _now()
    result = resolve_and_recompute(
        store, client, _account_metrics_config(client), now.date(), now
    )
    logger.info(
        "Recomputed: {} persons, {} orgs, {} metrics",
        result.person_count,
        result.org_count,
        result.metric_count,
    )
    store.close()


@main.command(name="add-email")
@click.option(
    "--db", "db_path", default=_DEFAULT_DB_PATH, help="Path to the SQLite store"
)
@click.option("--person", "person_id", required=True, help="Target person id")
@click.option(
    "--email", "email", required=True, help="Email address to attach to the person"
)
@click.option(
    "--calendar",
    "calendar_id",
    default=_DEFAULT_CALENDAR_ID,
    help="Calendar id to backfill from",
)
@click.option(
    "--gmail-max",
    default=_DEFAULT_GMAIL_MAX_MESSAGES,
    help="Max Gmail messages to backfill",
)
@click.option(
    "--backfill/--no-backfill",
    default=True,
    help="Fetch the address's history immediately",
)
@click.option("--reason", default=None, help="Why this email is being added")
def add_email_command(
    db_path: str,
    person_id: str,
    email: str,
    calendar_id: str,
    gmail_max: int,
    backfill: bool,
    reason: str | None,
) -> None:
    """Attach an email to a person and (by default) backfill its full Google history."""
    store = create_and_initialize_store(Path(db_path))
    now = _now()
    parsed_person_id = PersonId(person_id)
    parsed_email = EmailAddress(email)
    if backfill:
        client = LatchkeyGoogleApiClient()
        ingested = add_email_to_person_and_backfill(
            store,
            client,
            parsed_email,
            parsed_person_id,
            calendar_id,
            _account_metrics_config(client),
            now.date(),
            now,
            reason,
            gmail_max,
        )
        logger.info(
            "Added {} to {} and backfilled {} interactions",
            parsed_email,
            parsed_person_id,
            ingested,
        )
    else:
        add_email_to_person(store, parsed_email, parsed_person_id, reason, now)
        logger.info(
            "Added {} to {} (backfill deferred to next sync)",
            parsed_email,
            parsed_person_id,
        )
    store.close()


@main.command(name="show")
@click.option(
    "--db", "db_path", default=_DEFAULT_DB_PATH, help="Path to the SQLite store"
)
@click.option("--email", "email", required=True, help="An email of the person to show")
def show_command(db_path: str, email: str) -> None:
    """Show a person's resolved identity, metrics, and a few raw source records."""
    store = create_and_initialize_store(Path(db_path))
    target = EmailAddress(email)
    person_id_by_email = {
        link.email: link.person_id for link in store.get_all_person_email_links()
    }
    person_id = person_id_by_email.get(target)
    if person_id is None:
        click.echo(f"No person found for {target}")
        store.close()
        return
    payload = build_web_view_payload(store, _now().date())
    match = next(
        (entry for entry in payload["people"] if entry["person_id"] == str(person_id)),
        None,
    )
    click.echo(json.dumps(match, indent=2))
    store.close()


@main.command(name="export-web-payload")
@click.option(
    "--db", "db_path", default=_DEFAULT_DB_PATH, help="Path to the SQLite store"
)
@click.option(
    "--output",
    "output_path",
    default=None,
    help="Where to write the JSON (stdout if omitted)",
)
def export_web_payload_command(db_path: str, output_path: str | None) -> None:
    """Export the web-view JSON payload that the meeting-contacts view consumes."""
    store = create_and_initialize_store(Path(db_path))
    payload = build_web_view_payload(store, _now().date())
    serialized = json.dumps(payload, indent=2)
    if output_path is None:
        click.echo(serialized)
    else:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(serialized)
        logger.info(
            "Wrote web payload for {} people to {}", len(payload["people"]), output_path
        )
    store.close()
