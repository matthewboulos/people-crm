from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import AbstractSet, Final

from imbue.imbue_common.primitives import Probability
from imbue.imbue_common.pure import pure

from people_crm.data_types import (
    EmailIdentity,
    IdentityOverride,
    MergeReason,
    NameSource,
    Org,
    OrgDomain,
    OrgDomainSource,
    OverrideType,
    Person,
    PersonEmailLink,
)
from people_crm.ingest import infer_name_from_email
from people_crm.primitives import (
    FREE_EMAIL_DOMAINS,
    DisplayName,
    DomainName,
    EmailAddress,
    OrgId,
    OrgName,
    PersonId,
)

# Lower rank == more trustworthy provenance when choosing a person's best name.
_NAME_SOURCE_RANK: Final[Mapping[NameSource, int]] = {
    NameSource.MANUAL: 0,
    NameSource.CONTACTS: 1,
    NameSource.CALENDAR: 2,
    NameSource.GMAIL: 3,
    NameSource.INFERRED: 4,
    NameSource.INFERRED_WEAK: 5,
}

_MERGE_CONFIDENCE_BY_REASON: Final[Mapping[MergeReason, Probability | None]] = {
    MergeReason.MANUAL: None,
    MergeReason.SAME_CONTACT_ENTRY: Probability(0.95),
    MergeReason.SAME_STRONG_NAME: Probability(0.6),
    MergeReason.SINGLE_EMAIL: None,
}


@pure
def _find(
    parent: dict[EmailAddress, EmailAddress], email: EmailAddress
) -> EmailAddress:
    root = email
    while parent[root] != root:
        root = parent[root]
    # Path compression.
    walker = email
    while parent[walker] != root:
        parent[walker], walker = root, parent[walker]
    return root


@pure
def _union(
    parent: dict[EmailAddress, EmailAddress], left: EmailAddress, right: EmailAddress
) -> None:
    left_root = _find(parent, left)
    right_root = _find(parent, right)
    if left_root != right_root:
        # Keep the lexicographically smaller root for determinism.
        if left_root <= right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root


@pure
def _is_strong_name(identity: EmailIdentity) -> bool:
    if identity.observed_name is None or identity.observed_name_source is None:
        return False
    if identity.observed_name_source not in (
        NameSource.CALENDAR,
        NameSource.GMAIL,
        NameSource.CONTACTS,
    ):
        return False
    return len(identity.observed_name.split()) >= 2


@pure
def resolve_orgs(
    domains: Sequence[DomainName],
    seed_domain_to_org_name: Mapping[DomainName, OrgName],
    existing_orgs: Sequence[Org],
    now: datetime,
) -> tuple[list[Org], list[OrgDomain]]:
    """Build org entities and the domain->org map from seeds plus derived corporate domains."""
    existing_id_by_name: dict[str, OrgId] = {
        str(org.name): org.org_id for org in existing_orgs
    }
    existing_created_by_id: dict[OrgId, datetime] = {
        org.org_id: org.created_at for org in existing_orgs
    }
    org_by_name: dict[str, Org] = {}
    org_domains: list[OrgDomain] = []

    def _org_for_name(name: OrgName) -> Org:
        key = str(name)
        if key in org_by_name:
            return org_by_name[key]
        org_id = existing_id_by_name.get(key, OrgId.generate())
        created_at = existing_created_by_id.get(org_id, now)
        org = Org(org_id=org_id, name=name, created_at=created_at, updated_at=now)
        org_by_name[key] = org
        return org

    # Seed domains first (they carry curated labels), then derived corporate
    # domains named after themselves. Free-email domains are never mapped.
    for domain, org_name in sorted(seed_domain_to_org_name.items()):
        org = _org_for_name(org_name)
        org_domains.append(
            OrgDomain(domain=domain, org_id=org.org_id, source=OrgDomainSource.SEED)
        )

    mapped_domains = {str(od.domain) for od in org_domains}
    for domain in sorted(set(domains)):
        if str(domain) in FREE_EMAIL_DOMAINS or str(domain) in mapped_domains:
            continue
        org = _org_for_name(OrgName(str(domain)))
        org_domains.append(
            OrgDomain(domain=domain, org_id=org.org_id, source=OrgDomainSource.DERIVED)
        )
        mapped_domains.add(str(domain))

    return list(org_by_name.values()), org_domains


@pure
def resolve_person_graph(
    identities: Sequence[EmailIdentity],
    contact_groups: Sequence[Sequence[EmailAddress]],
    overrides: Sequence[IdentityOverride],
    existing_persons: Sequence[Person],
    existing_links: Sequence[PersonEmailLink],
    person_ids_with_user_data: AbstractSet[PersonId],
    org_id_by_domain: Mapping[DomainName, OrgId],
    now: datetime,
) -> tuple[list[Person], list[PersonEmailLink]]:
    """Group email identities into stable persons using contacts, strong names, and manual overrides."""
    identity_by_email: dict[EmailAddress, EmailIdentity] = {
        identity.email: identity for identity in identities
    }
    existing_person_id_by_email: dict[EmailAddress, PersonId] = {
        link.email: link.person_id for link in existing_links
    }
    existing_created_by_person: dict[PersonId, datetime] = {
        p.person_id: p.created_at for p in existing_persons
    }

    split_emails = {
        o.email for o in overrides if o.override_type == OverrideType.SPLIT_EMAIL
    }
    forced_person_id_by_email: dict[EmailAddress, PersonId] = {
        o.email: o.forced_person_id
        for o in overrides
        if o.override_type == OverrideType.ADD_EMAIL_TO_PERSON
        and o.forced_person_id is not None
    }

    # Universe of emails we will assign to persons.
    universe: set[EmailAddress] = (
        set(identity_by_email.keys())
        | set(forced_person_id_by_email.keys())
        | split_emails
    )
    parent: dict[EmailAddress, EmailAddress] = {email: email for email in universe}

    def _union_if_allowed(left: EmailAddress, right: EmailAddress) -> None:
        if left in split_emails or right in split_emails:
            return
        if left in parent and right in parent:
            _union(parent, left, right)

    contact_group_emails: set[EmailAddress] = set()
    for group in contact_groups:
        present = [email for email in group if email in parent]
        for email in present:
            contact_group_emails.add(email)
        for other in present[1:]:
            _union_if_allowed(present[0], other)

    # Strong-name merge: identical multi-token name across differing domains.
    emails_by_name_key: dict[str, list[EmailAddress]] = {}
    for identity in identities:
        if identity.email in split_emails or not _is_strong_name(identity):
            continue
        assert identity.observed_name is not None
        name_key = " ".join(identity.observed_name.lower().split())
        emails_by_name_key.setdefault(name_key, []).append(identity.email)
    strong_name_emails: set[EmailAddress] = set()
    for name_group in emails_by_name_key.values():
        distinct_domains = {identity_by_email[email].domain for email in name_group}
        if len(name_group) >= 2 and len(distinct_domains) >= 2:
            for email in name_group:
                strong_name_emails.add(email)
            for other in name_group[1:]:
                _union_if_allowed(name_group[0], other)

    # Manual add-email overrides win: union the added email with the target's
    # current emails so they land in one person.
    for email, forced_person_id in forced_person_id_by_email.items():
        target_emails = [
            e
            for e, pid in existing_person_id_by_email.items()
            if pid == forced_person_id and e in parent
        ]
        for target_email in target_emails:
            _union(parent, email, target_email)

    # Collect groups by root.
    emails_by_root: dict[EmailAddress, list[EmailAddress]] = {}
    for email in sorted(universe):
        emails_by_root.setdefault(_find(parent, email), []).append(email)

    persons: list[Person] = []
    links: list[PersonEmailLink] = []
    # Assign person ids so each is used by at most one group. Process the groups
    # with the strongest claim on an existing id first (manual overrides, then
    # most overlap with an existing person, then larger groups), so the rightful
    # owner keeps the stable id and any regrouped remainder gets a fresh one.
    used_person_ids: set[PersonId] = set()

    def _group_priority(group: Sequence[EmailAddress]) -> tuple[int, int, int, str]:
        has_forced = any(email in forced_person_id_by_email for email in group)
        existing_overlap = sum(
            1 for email in group if email in existing_person_id_by_email
        )
        return (0 if has_forced else 1, -existing_overlap, -len(group), min(group))

    for group in sorted(emails_by_root.values(), key=_group_priority):
        person_id = _choose_person_id(
            group,
            forced_person_id_by_email,
            existing_person_id_by_email,
            person_ids_with_user_data,
            used_person_ids,
        )
        used_person_ids.add(person_id)
        canonical_email, display_name, display_name_source = _choose_identity(
            group, identity_by_email
        )
        org_id = _org_for_email(canonical_email, org_id_by_domain)
        created_at = existing_created_by_person.get(person_id, now)
        persons.append(
            Person(
                person_id=person_id,
                canonical_email=canonical_email,
                display_name=display_name,
                display_name_source=display_name_source,
                org_id=org_id,
                created_at=created_at,
                updated_at=now,
            )
        )
        for email in group:
            reason = _merge_reason_for_email(
                email,
                len(group),
                forced_person_id_by_email,
                contact_group_emails,
                strong_name_emails,
            )
            links.append(
                PersonEmailLink(
                    email=email,
                    person_id=person_id,
                    merge_reason=reason,
                    merge_confidence=_MERGE_CONFIDENCE_BY_REASON[reason],
                    merged_at=now,
                )
            )

    return persons, links


@pure
def _choose_person_id(
    group: Sequence[EmailAddress],
    forced_person_id_by_email: Mapping[EmailAddress, PersonId],
    existing_person_id_by_email: Mapping[EmailAddress, PersonId],
    person_ids_with_user_data: AbstractSet[PersonId],
    used_person_ids: AbstractSet[PersonId],
) -> PersonId:
    """Pick a stable id for this group, never reusing one already taken by another group."""
    forced = sorted(
        {
            forced_person_id_by_email[email]
            for email in group
            if email in forced_person_id_by_email
        }
    )
    for forced_id in forced:
        if forced_id not in used_person_ids:
            return forced_id
    existing_ids = [
        existing_person_id_by_email[email]
        for email in group
        if email in existing_person_id_by_email
    ]
    if existing_ids:
        # Prefer an id that already carries user data; then the id covering the
        # most emails; then lexicographically for determinism.
        def _rank(person_id: PersonId) -> tuple[int, int, str]:
            carries_user_data = 0 if person_id in person_ids_with_user_data else 1
            return (carries_user_data, -existing_ids.count(person_id), str(person_id))

        for person_id in sorted(set(existing_ids), key=_rank):
            if person_id not in used_person_ids:
                return person_id
    # Every candidate id is already claimed by another group -> mint a fresh one.
    new_id = PersonId.generate()
    while new_id in used_person_ids:
        new_id = PersonId.generate()
    return new_id


@pure
def _choose_identity(
    group: Sequence[EmailAddress],
    identity_by_email: Mapping[EmailAddress, EmailIdentity],
) -> tuple[EmailAddress, DisplayName | None, NameSource | None]:
    def _rank(email: EmailAddress) -> tuple[int, int, str]:
        identity = identity_by_email.get(email)
        name_rank = (
            _NAME_SOURCE_RANK[identity.observed_name_source]
            if identity is not None and identity.observed_name_source is not None
            else 99
        )
        is_free = 1 if email.split("@")[-1] in FREE_EMAIL_DOMAINS else 0
        return (name_rank, is_free, str(email))

    best_email = sorted(group, key=_rank)[0]
    best_identity = identity_by_email.get(best_email)
    if best_identity is not None and best_identity.observed_name is not None:
        return (
            best_email,
            best_identity.observed_name,
            best_identity.observed_name_source,
        )

    inferred = infer_name_from_email(str(best_email))
    if inferred is not None:
        return best_email, DisplayName(inferred[0]), inferred[1]
    return best_email, None, None


@pure
def _org_for_email(
    email: EmailAddress, org_id_by_domain: Mapping[DomainName, OrgId]
) -> OrgId | None:
    domain = DomainName(email.split("@")[-1])
    if str(domain) in FREE_EMAIL_DOMAINS:
        return None
    return org_id_by_domain.get(domain)


@pure
def _merge_reason_for_email(
    email: EmailAddress,
    group_size: int,
    forced_person_id_by_email: Mapping[EmailAddress, PersonId],
    contact_group_emails: AbstractSet[EmailAddress],
    strong_name_emails: AbstractSet[EmailAddress],
) -> MergeReason:
    if email in forced_person_id_by_email:
        return MergeReason.MANUAL
    if group_size <= 1:
        return MergeReason.SINGLE_EMAIL
    if email in contact_group_emails:
        return MergeReason.SAME_CONTACT_ENTRY
    if email in strong_name_emails:
        return MergeReason.SAME_STRONG_NAME
    return MergeReason.SINGLE_EMAIL
