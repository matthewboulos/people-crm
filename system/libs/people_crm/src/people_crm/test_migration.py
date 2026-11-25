import json
from datetime import date, datetime, timezone
from pathlib import Path

from people_crm.migration import migrate_legacy_data
from people_crm.store import SqliteCrmStore
from people_crm.web_view import build_web_view_payload

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def _write_legacy_files(directory: Path) -> tuple[Path, Path]:
    contacts = {
        "today": "2026-07-06",
        "seed_org_domains": {"example.org": "Example Org", "example.net": "Example Org"},
        "people": [
            {
                "email": "jordan.lee@example.net",
                "name": "Jordan Lee",
                "name_source": "calendar",
                "first": "2023-12-12",
                "last": "2026-07-06",
            },
            {
                "email": "unnamed@example.com",
                "name": None,
                "name_source": None,
                "first": "2024-01-01",
                "last": "2024-01-01",
            },
        ],
    }
    filters = {
        "domains": [{"domain": "example.org", "label": "Example Org", "source": "seed"}],
        "emails": [],
    }
    contacts_path = directory / "all_contacts.json"
    filters_path = directory / "filters.json"
    contacts_path.write_text(json.dumps(contacts))
    filters_path.write_text(json.dumps(filters))
    return contacts_path, filters_path


def test_migration_seeds_identities_filters_and_orgs(
    crm_store: SqliteCrmStore, tmp_path: Path
) -> None:
    contacts_path, filters_path = _write_legacy_files(tmp_path)

    result = migrate_legacy_data(crm_store, contacts_path, filters_path, _NOW)

    assert result.email_identities_seeded == 2
    assert result.filter_rules_seeded == 1
    # Two seed domains collapse to a single "Example Org" org.
    assert result.orgs_seeded == 1
    assert result.org_domains_seeded == 2

    identities_by_email = {
        str(i.email): i for i in crm_store.get_all_email_identities()
    }
    assert (
        str(identities_by_email["jordan.lee@example.net"].observed_name)
        == "Jordan Lee"
    )
    assert identities_by_email["unnamed@example.com"].observed_name is None


def test_migrated_filters_appear_in_web_view_payload(
    crm_store: SqliteCrmStore, tmp_path: Path
) -> None:
    contacts_path, filters_path = _write_legacy_files(tmp_path)
    migrate_legacy_data(crm_store, contacts_path, filters_path, _NOW)

    payload = build_web_view_payload(crm_store, date(2026, 7, 6))

    domain_rules = payload["filters"]["domains"]
    seed_rule = next(rule for rule in domain_rules if rule["domain"] == "example.org")
    assert seed_rule["label"] == "Example Org"
    assert seed_rule["source"] == "SEED"
