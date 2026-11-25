import json
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Final

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.pure import pure
from loguru import logger

from people_crm.data_types import (
    EmailIdentity,
    FilterRule,
    FilterRuleSource,
    FilterRuleType,
    MigrationResult,
    NameSource,
    Org,
    OrgDomain,
    OrgDomainSource,
)
from people_crm.errors import MigrationError
from people_crm.interfaces import CrmStoreInterface
from people_crm.primitives import DisplayName, DomainName, EmailAddress, OrgId, OrgName

_NAME_SOURCE_BY_LEGACY_VALUE: Final[Mapping[str, NameSource]] = {
    "calendar": NameSource.CALENDAR,
    "gmail": NameSource.GMAIL,
    "contacts": NameSource.CONTACTS,
    "inferred": NameSource.INFERRED,
    "inferred_weak": NameSource.INFERRED_WEAK,
}


@pure
def _legacy_date_to_datetime(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        return fallback
    return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except OSError as e:
        raise MigrationError(f"Cannot read legacy file: {path}") from e
    except json.JSONDecodeError as e:
        raise MigrationError(f"Invalid JSON in legacy file: {path}") from e


def _seed_email_identities(
    store: CrmStoreInterface, people: list[Mapping[str, Any]], now: datetime
) -> int:
    seeded = 0
    for person in people:
        raw_email = person.get("email")
        if not raw_email:
            continue
        try:
            email = EmailAddress(raw_email)
        except ValueError:
            logger.warning("Skipping malformed legacy email {!r}", raw_email)
            continue
        raw_name = person.get("name")
        name_source = _NAME_SOURCE_BY_LEGACY_VALUE.get(person.get("name_source") or "")
        first_seen = _legacy_date_to_datetime(person.get("first"), now)
        last_seen = _legacy_date_to_datetime(person.get("last"), now)
        identity = EmailIdentity(
            email=email,
            domain=email.domain,
            observed_name=DisplayName(raw_name) if raw_name else None,
            observed_name_source=name_source if raw_name else None,
            is_free_email_domain=email.is_free_email,
            first_seen_at=first_seen,
            last_seen_at=last_seen,
        )
        store.upsert_email_identity(identity)
        seeded += 1
    return seeded


def _seed_filter_rules(store: CrmStoreInterface, filters: Mapping[str, Any]) -> int:
    seeded = 0
    for domain_rule in filters.get("domains", []):
        store.upsert_filter_rule(
            FilterRule(
                rule_type=FilterRuleType.DOMAIN,
                value=domain_rule["domain"],
                label=domain_rule.get("label"),
                source=FilterRuleSource.SEED,
            )
        )
        seeded += 1
    for email_rule in filters.get("emails", []):
        value = email_rule if isinstance(email_rule, str) else email_rule.get("email")
        if not value:
            continue
        store.upsert_filter_rule(
            FilterRule(
                rule_type=FilterRuleType.EMAIL,
                value=value,
                label=None,
                source=FilterRuleSource.SEED,
            )
        )
        seeded += 1
    return seeded


def _seed_orgs(
    store: CrmStoreInterface, seed_org_domains: Mapping[str, str], now: datetime
) -> tuple[int, int]:
    org_id_by_name: dict[str, OrgId] = {}
    orgs_seeded = 0
    domains_seeded = 0
    for raw_domain, raw_label in sorted(seed_org_domains.items()):
        try:
            domain = DomainName(raw_domain)
        except ValueError:
            continue
        org_name = OrgName(raw_label)
        if str(org_name) not in org_id_by_name:
            org_id = OrgId.generate()
            org_id_by_name[str(org_name)] = org_id
            store.upsert_org(
                Org(org_id=org_id, name=org_name, created_at=now, updated_at=now)
            )
            orgs_seeded += 1
        store.upsert_org_domain(
            OrgDomain(
                domain=domain,
                org_id=org_id_by_name[str(org_name)],
                source=OrgDomainSource.SEED,
            )
        )
        domains_seeded += 1
    return orgs_seeded, domains_seeded


def migrate_legacy_data(
    store: CrmStoreInterface, all_contacts_path: Path, filters_path: Path, now: datetime
) -> MigrationResult:
    """Seed names, exclusion rules, and org seeds from the existing meeting-contacts JSON (nothing lost)."""
    contacts_data = _load_json(all_contacts_path)
    filters_data = _load_json(filters_path)

    identities_seeded = _seed_email_identities(
        store, contacts_data.get("people", []), now
    )
    filter_rules_seeded = _seed_filter_rules(store, filters_data)
    orgs_seeded, domains_seeded = _seed_orgs(
        store, contacts_data.get("seed_org_domains", {}), now
    )

    return MigrationResult(
        email_identities_seeded=NonNegativeInt(identities_seeded),
        filter_rules_seeded=NonNegativeInt(filter_rules_seeded),
        orgs_seeded=NonNegativeInt(orgs_seeded),
        org_domains_seeded=NonNegativeInt(domains_seeded),
    )
