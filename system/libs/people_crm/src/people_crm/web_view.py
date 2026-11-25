from collections import defaultdict
from datetime import date
from typing import Any, Final

from people_crm.data_types import (
    FilterRuleType,
    Interaction,
    ParticipantRole,
    PersonMetric,
)
from people_crm.interfaces import CrmStoreInterface
from people_crm.primitives import EmailAddress, PersonId

_MAX_SAMPLES_PER_PERSON: Final[int] = 6


def _samples_by_person(
    interactions: list[Interaction], email_to_person_id: dict[EmailAddress, PersonId]
) -> dict[PersonId, list[dict[str, Any]]]:
    # Preserve + surface: each person keeps links back to the raw source records.
    ordered = sorted(
        interactions, key=lambda interaction: interaction.occurred_at, reverse=True
    )
    samples: dict[PersonId, list[dict[str, Any]]] = defaultdict(list)
    seen_refs: dict[PersonId, set[str]] = defaultdict(set)
    for interaction in ordered:
        person_ids_here: set[PersonId] = set()
        for participant in interaction.participants:
            if participant.role == ParticipantRole.SELF:
                continue
            person_id = email_to_person_id.get(participant.email)
            if person_id is not None:
                person_ids_here.add(person_id)
        for person_id in person_ids_here:
            if len(samples[person_id]) >= _MAX_SAMPLES_PER_PERSON:
                continue
            if str(interaction.source_ref) in seen_refs[person_id]:
                continue
            seen_refs[person_id].add(str(interaction.source_ref))
            samples[person_id].append(
                {
                    "summary": interaction.title,
                    "date": interaction.occurred_at.date().isoformat(),
                    "type": interaction.interaction_type.value,
                    "duration_min": interaction.duration_minutes,
                    "source_ref": str(interaction.source_ref),
                    "source_link": interaction.source_link,
                }
            )
    return samples


def build_web_view_payload(store: CrmStoreInterface, today: date) -> dict[str, Any]:
    """Build the JSON payload the meeting-contacts web view consumes, computed from the store."""
    persons = store.get_all_persons()
    links = store.get_all_person_email_links()
    orgs = store.get_all_orgs()
    filter_rules = store.get_all_filter_rules()
    metrics_by_person: dict[PersonId, PersonMetric] = {
        m.person_id: m for m in store.get_all_person_metrics()
    }

    org_name_by_id = {org.org_id: str(org.name) for org in orgs}
    email_to_person_id = {link.email: link.person_id for link in links}
    emails_by_person: dict[PersonId, list[str]] = defaultdict(list)
    for link in links:
        emails_by_person[link.person_id].append(str(link.email))

    samples = _samples_by_person(store.get_all_interactions(), email_to_person_id)

    people: list[dict[str, Any]] = []
    for person in persons:
        metric = metrics_by_person.get(person.person_id)
        default_org = (
            org_name_by_id.get(person.org_id) if person.org_id is not None else None
        )
        people.append(
            {
                "person_id": str(person.person_id),
                "name": str(person.display_name)
                if person.display_name is not None
                else None,
                "name_source": person.display_name_source.value
                if person.display_name_source
                else None,
                "email": str(person.canonical_email),
                "emails": sorted(emails_by_person.get(person.person_id, [])),
                "domain": person.canonical_email.split("@")[-1],
                "default_org": default_org,
                "count": int(metric.meeting_count) if metric else 0,
                "one_on_one": int(metric.one_on_one_count) if metric else 0,
                "small_group": int(metric.focused_count) if metric else 0,
                "hours_total": float(metric.hours_total) if metric else 0.0,
                "hours_focused": float(metric.hours_focused) if metric else 0.0,
                "first": metric.first_interaction_date.isoformat()
                if metric and metric.first_interaction_date
                else None,
                "last": metric.last_interaction_date.isoformat()
                if metric and metric.last_interaction_date
                else None,
                "days_since_last": int(metric.days_since_last)
                if metric and metric.days_since_last is not None
                else None,
                "group_only": metric.is_group_only if metric else True,
                "samples": samples.get(person.person_id, []),
            }
        )

    # Default ranking mirrors the prototype: people actually met (small-group) first.
    people.sort(
        key=lambda entry: (-entry["small_group"], -entry["one_on_one"], -entry["count"])
    )

    filters_payload = {
        "domains": [
            {"domain": rule.value, "label": rule.label, "source": rule.source.value}
            for rule in filter_rules
            if rule.rule_type == FilterRuleType.DOMAIN
        ],
        "emails": [
            rule.value
            for rule in filter_rules
            if rule.rule_type == FilterRuleType.EMAIL
        ],
    }

    return {"today": today.isoformat(), "people": people, "filters": filters_payload}
