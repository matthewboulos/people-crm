from datetime import datetime, timezone

from people_crm.data_types import (
    BackfillStatus,
    EmailIdentity,
    IdentityOverride,
    MergeReason,
    NameSource,
    OrgDomainSource,
    OverrideType,
    Person,
    PersonEmailLink,
)
from people_crm.identity import resolve_orgs, resolve_person_graph
from people_crm.primitives import DomainName, EmailAddress, OrgName, PersonId

_NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def _ident(email: str, name: str | None, source: NameSource | None) -> EmailIdentity:
    address = EmailAddress(email)
    return EmailIdentity(
        email=address,
        domain=address.domain,
        observed_name=name,  # type: ignore[arg-type]
        observed_name_source=source,
        is_free_email_domain=address.is_free_email,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
    )


def _person_id_by_email(links: list[PersonEmailLink]) -> dict[str, str]:
    return {str(link.email): str(link.person_id) for link in links}


def test_distinct_emails_become_separate_persons() -> None:
    identities = [_ident("a@x.com", None, None), _ident("b@y.com", None, None)]

    persons, links = resolve_person_graph(identities, [], [], [], [], set(), {}, _NOW)

    assert len(persons) == 2
    assert all(link.merge_reason == MergeReason.SINGLE_EMAIL for link in links)


def test_contact_group_merges_emails_into_one_person() -> None:
    identities = [
        _ident("work@example.org", "Jane Doe", NameSource.CALENDAR),
        _ident("jane@gmail.com", "Jane Doe", NameSource.GMAIL),
    ]
    contact_groups = [[EmailAddress("work@example.org"), EmailAddress("jane@gmail.com")]]

    persons, links = resolve_person_graph(
        identities, contact_groups, [], [], [], set(), {}, _NOW
    )

    assert len(persons) == 1
    mapping = _person_id_by_email(links)
    assert mapping["work@example.org"] == mapping["jane@gmail.com"]
    assert all(link.merge_reason == MergeReason.SAME_CONTACT_ENTRY for link in links)


def test_strong_name_across_domains_merges() -> None:
    identities = [
        _ident("work@example.org", "Jane Doe", NameSource.CALENDAR),
        _ident("jane@gmail.com", "Jane Doe", NameSource.GMAIL),
    ]

    persons, links = resolve_person_graph(identities, [], [], [], [], set(), {}, _NOW)

    assert len(persons) == 1
    assert all(link.merge_reason == MergeReason.SAME_STRONG_NAME for link in links)


def test_same_name_same_domain_does_not_merge() -> None:
    identities = [
        _ident("john1@x.com", "John Smith", NameSource.CALENDAR),
        _ident("john2@x.com", "John Smith", NameSource.CALENDAR),
    ]

    persons, _links = resolve_person_graph(identities, [], [], [], [], set(), {}, _NOW)

    assert len(persons) == 2


def test_single_token_name_does_not_merge() -> None:
    identities = [
        _ident("sf@example.org", "SF", NameSource.CALENDAR),
        _ident("sf@gmail.com", "SF", NameSource.CALENDAR),
    ]

    persons, _links = resolve_person_graph(identities, [], [], [], [], set(), {}, _NOW)

    assert len(persons) == 2


def test_add_email_override_forces_into_existing_person_and_reuses_id() -> None:
    person_id = PersonId.generate()
    existing_person = Person(
        person_id=person_id,
        canonical_email=EmailAddress("work@example.org"),
        display_name=None,
        display_name_source=None,
        org_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    existing_link = PersonEmailLink(
        email=EmailAddress("work@example.org"),
        person_id=person_id,
        merge_reason=MergeReason.SINGLE_EMAIL,
        merge_confidence=None,
        merged_at=_NOW,
    )
    identities = [
        _ident("work@example.org", None, None),
        _ident("personal@gmail.com", None, None),
    ]
    override = IdentityOverride(
        email=EmailAddress("personal@gmail.com"),
        override_type=OverrideType.ADD_EMAIL_TO_PERSON,
        forced_person_id=person_id,
        reason="same human",
        backfill_status=BackfillStatus.PENDING,
        backfilled_at=None,
        created_at=_NOW,
    )

    persons, links = resolve_person_graph(
        identities, [], [override], [existing_person], [existing_link], set(), {}, _NOW
    )

    assert len(persons) == 1
    assert persons[0].person_id == person_id
    mapping = _person_id_by_email(links)
    assert mapping["personal@gmail.com"] == str(person_id)
    personal_link = next(
        link for link in links if str(link.email) == "personal@gmail.com"
    )
    assert personal_link.merge_reason == MergeReason.MANUAL


def test_regrouped_emails_do_not_reuse_the_same_person_id() -> None:
    # Two addresses were previously one person (id X). New data gives no reason to
    # keep them merged, so they split -- and must get distinct ids, not both X.
    identities = [_ident("a@x.com", None, None), _ident("b@y.com", None, None)]
    prior_id = PersonId.generate()
    existing_person = Person(
        person_id=prior_id,
        canonical_email=EmailAddress("a@x.com"),
        display_name=None,
        display_name_source=None,
        org_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    existing_links = [
        PersonEmailLink(
            email=EmailAddress("a@x.com"), person_id=prior_id,
            merge_reason=MergeReason.SAME_CONTACT_ENTRY, merge_confidence=None, merged_at=_NOW,
        ),
        PersonEmailLink(
            email=EmailAddress("b@y.com"), person_id=prior_id,
            merge_reason=MergeReason.SAME_CONTACT_ENTRY, merge_confidence=None, merged_at=_NOW,
        ),
    ]

    persons, links = resolve_person_graph(
        identities, [], [], [existing_person], existing_links, set(), {}, _NOW
    )

    assert len(persons) == 2
    ids = {p.person_id for p in persons}
    assert len(ids) == 2  # no duplicate id -> no UNIQUE constraint violation
    assert prior_id in ids  # the stable id is preserved for one of them


def test_split_override_detaches_email_despite_contact_group() -> None:
    identities = [
        _ident("work@example.org", "Jane Doe", NameSource.CALENDAR),
        _ident("jane@gmail.com", "Jane Doe", NameSource.GMAIL),
    ]
    contact_groups = [[EmailAddress("work@example.org"), EmailAddress("jane@gmail.com")]]
    override = IdentityOverride(
        email=EmailAddress("jane@gmail.com"),
        override_type=OverrideType.SPLIT_EMAIL,
        forced_person_id=None,
        reason="different person",
        backfill_status=BackfillStatus.NONE,
        backfilled_at=None,
        created_at=_NOW,
    )

    persons, links = resolve_person_graph(
        identities, contact_groups, [override], [], [], set(), {}, _NOW
    )

    assert len(persons) == 2
    mapping = _person_id_by_email(links)
    assert mapping["work@example.org"] != mapping["jane@gmail.com"]


def test_resolve_orgs_maps_seeds_and_derived_but_not_free_email() -> None:
    domains = [
        DomainName("example.org"),
        DomainName("gmail.com"),
        DomainName("milltownpartners.com"),
    ]
    seed_map = {DomainName("example.org"): OrgName("Example Org")}

    orgs, org_domains = resolve_orgs(domains, seed_map, [], _NOW)

    org_name_by_domain = {
        str(od.domain): next(o.name for o in orgs if o.org_id == od.org_id)
        for od in org_domains
    }
    assert org_name_by_domain["example.org"] == "Example Org"
    assert org_name_by_domain["milltownpartners.com"] == "milltownpartners.com"
    assert "gmail.com" not in org_name_by_domain
    source_by_domain = {str(od.domain): od.source for od in org_domains}
    assert source_by_domain["example.org"] == OrgDomainSource.SEED
    assert source_by_domain["milltownpartners.com"] == OrgDomainSource.DERIVED
