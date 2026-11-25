from datetime import date, datetime
from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonNegativeFloat, NonNegativeInt, Probability
from pydantic import Field

from people_crm.primitives import (
    DisplayName,
    DomainName,
    EmailAddress,
    FeedbackId,
    InteractionId,
    OrgId,
    OrgName,
    PersonId,
    SourceRef,
)

# --- Enums ---


class InteractionType(UpperCaseStrEnum):
    """The kind of interaction an edge represents."""

    CALENDAR = auto()
    EMAIL = auto()


class SourceSystem(UpperCaseStrEnum):
    """The external system an interaction was ingested from."""

    GOOGLE_CALENDAR = auto()
    GMAIL = auto()


class ParticipantRole(UpperCaseStrEnum):
    """The role an email identity played in an interaction."""

    SELF = auto()
    ORGANIZER = auto()
    ATTENDEE = auto()
    SENDER = auto()
    RECIPIENT_TO = auto()
    RECIPIENT_CC = auto()


class EmailDirection(UpperCaseStrEnum):
    """Whether an email was sent by the user or received."""

    SENT = auto()
    RECEIVED = auto()


class ResponseStatus(UpperCaseStrEnum):
    """A calendar attendee's response status."""

    ACCEPTED = auto()
    DECLINED = auto()
    TENTATIVE = auto()
    NEEDS_ACTION = auto()


class NameSource(UpperCaseStrEnum):
    """Where a display name was resolved from (provenance)."""

    CALENDAR = auto()
    GMAIL = auto()
    CONTACTS = auto()
    INFERRED = auto()
    INFERRED_WEAK = auto()
    MANUAL = auto()


class MergeReason(UpperCaseStrEnum):
    """Why an email was mapped to a given person (auditable merge record)."""

    SINGLE_EMAIL = auto()
    SAME_CONTACT_ENTRY = auto()
    SAME_STRONG_NAME = auto()
    MANUAL = auto()


class OrgDomainSource(UpperCaseStrEnum):
    """Where a domain->org mapping came from."""

    SEED = auto()
    DERIVED = auto()
    MANUAL = auto()


class OverrideType(UpperCaseStrEnum):
    """The kind of manual identity override."""

    ADD_EMAIL_TO_PERSON = auto()
    SPLIT_EMAIL = auto()


class BackfillStatus(UpperCaseStrEnum):
    """Progress of an on-demand backfill for a manually added email."""

    NONE = auto()
    PENDING = auto()
    DONE = auto()
    FAILED = auto()


class FilterRuleType(UpperCaseStrEnum):
    """Whether an exclusion rule targets a domain or a specific email."""

    DOMAIN = auto()
    EMAIL = auto()


class FilterRuleSource(UpperCaseStrEnum):
    """Where an exclusion rule came from."""

    SEED = auto()
    USER = auto()


class FeedbackSignal(UpperCaseStrEnum):
    """The binary signal attached to a piece of category feedback."""

    POSITIVE = auto()
    NEGATIVE = auto()


# --- Source layer ---


class EmailIdentity(FrozenModel):
    """A distinct email address observed in the source data (source layer)."""

    email: EmailAddress = Field(
        description="The lower-cased email address (natural key)"
    )
    domain: DomainName = Field(description="The domain portion of the address")
    observed_name: DisplayName | None = Field(
        description="Best display name observed for this address"
    )
    observed_name_source: NameSource | None = Field(
        description="Provenance of the observed name"
    )
    is_free_email_domain: bool = Field(
        description="Whether the domain is a consumer mailbox provider"
    )
    first_seen_at: datetime = Field(
        description="Earliest interaction time this address appeared in"
    )
    last_seen_at: datetime = Field(
        description="Latest interaction time this address appeared in"
    )


class InteractionParticipant(FrozenModel):
    """A single email identity's participation in one interaction (a source-layer edge)."""

    email: EmailAddress = Field(description="The participant's email address")
    role: ParticipantRole = Field(
        description="The participant's role in the interaction"
    )
    response_status: ResponseStatus | None = Field(
        description="Calendar RSVP status, if applicable"
    )


class Interaction(FrozenModel):
    """A single raw calendar event or email message with its full participant list (source layer)."""

    interaction_id: InteractionId = Field(
        description="Our stable id for this interaction"
    )
    interaction_type: InteractionType = Field(description="Calendar or email")
    source_system: SourceSystem = Field(
        description="Which Google system this came from"
    )
    source_ref: SourceRef = Field(
        description="The source system's stable id (eventId / messageId)"
    )
    source_link: str | None = Field(description="Deep link back to the origin record")
    occurred_at: datetime = Field(description="When the interaction occurred (UTC)")
    duration_minutes: NonNegativeInt | None = Field(
        description="Duration in minutes (calendar only)"
    )
    title: str = Field(description="Event summary or email subject")
    recurring_series_ref: str | None = Field(
        description="The recurring series id, if part of one"
    )
    direction: EmailDirection | None = Field(description="Sent/received (email only)")
    participants: tuple[InteractionParticipant, ...] = Field(
        description="Full participant list"
    )
    raw_payload: str = Field(description="The full raw source JSON, preserved verbatim")
    ingested_at: datetime = Field(
        description="When this interaction was first ingested (UTC)"
    )


# --- Derived layer ---


class Org(FrozenModel):
    """An organization people can be linked to (derived layer)."""

    org_id: OrgId = Field(description="Our stable id for this org")
    name: OrgName = Field(description="The org's display name")
    created_at: datetime = Field(description="When this org was first created")
    updated_at: datetime = Field(description="When this org was last updated")


class OrgDomain(FrozenModel):
    """A mapping from an email domain to an org (derived layer)."""

    domain: DomainName = Field(description="The email domain")
    org_id: OrgId = Field(description="The org this domain belongs to")
    source: OrgDomainSource = Field(description="How this mapping was established")


class Person(FrozenModel):
    """A merged human, possibly spanning several email identities (derived layer)."""

    person_id: PersonId = Field(description="Our stable id for this person")
    canonical_email: EmailAddress = Field(
        description="The primary email address for this person"
    )
    display_name: DisplayName | None = Field(
        description="Best display name for this person"
    )
    display_name_source: NameSource | None = Field(
        description="Provenance of the display name"
    )
    org_id: OrgId | None = Field(description="The org this person belongs to, if any")
    created_at: datetime = Field(description="When this person was first created")
    updated_at: datetime = Field(description="When this person was last updated")


class PersonEmailLink(FrozenModel):
    """The email->person mapping plus the auditable reason it was merged (derived layer)."""

    email: EmailAddress = Field(description="The email address")
    person_id: PersonId = Field(description="The person this email belongs to")
    merge_reason: MergeReason = Field(
        description="Why this email was mapped to this person"
    )
    merge_confidence: Probability | None = Field(
        description="Confidence in the merge, if automatic"
    )
    merged_at: datetime = Field(description="When this mapping was established")


class PersonMetric(FrozenModel):
    """Derived per-person metrics, recomputed from the interaction log (derived layer)."""

    person_id: PersonId = Field(description="The person these metrics describe")
    meeting_count: NonNegativeInt = Field(description="Total counted meetings")
    one_on_one_count: NonNegativeInt = Field(
        description="Meetings with exactly one other person"
    )
    focused_count: NonNegativeInt = Field(
        description="Small, non-social meetings actually attended"
    )
    minutes_total: NonNegativeInt = Field(
        description="Total minutes across counted meetings"
    )
    minutes_focused: NonNegativeInt = Field(
        description="Minutes across focused meetings"
    )
    first_interaction_date: date | None = Field(
        description="Date of the first interaction"
    )
    last_interaction_date: date | None = Field(
        description="Date of the most recent interaction"
    )
    days_since_last: NonNegativeInt | None = Field(
        description="Days since the last interaction"
    )
    is_group_only: bool = Field(
        description="Whether this person was never met in a small setting"
    )
    computed_at: datetime = Field(description="When these metrics were computed")

    @property
    def hours_total(self) -> NonNegativeFloat:
        return NonNegativeFloat(round(self.minutes_total / 60, 1))

    @property
    def hours_focused(self) -> NonNegativeFloat:
        return NonNegativeFloat(round(self.minutes_focused / 60, 1))


# --- User-data layer ---


class PersonUserData(FrozenModel):
    """User-owned free-form data attached to a person (user-data layer)."""

    person_id: PersonId = Field(description="The person this data belongs to")
    tier: str | None = Field(description="A user-chosen relationship tier")
    notes: str | None = Field(description="Free-form user notes")
    updated_at: datetime = Field(description="When the user last edited this")


class PersonTag(FrozenModel):
    """A user-applied binary category tag on a person (user-data layer)."""

    person_id: PersonId = Field(description="The person being tagged")
    tag: str = Field(description="The tag value")


class CategoryFeedback(FrozenModel):
    """A binary signal plus optional free-form note routed through an LLM judge (user-data layer)."""

    feedback_id: FeedbackId = Field(description="Our id for this feedback item")
    person_id: PersonId = Field(description="The person the feedback is about")
    signal: FeedbackSignal = Field(description="The binary signal")
    note: str | None = Field(description="Free-form note interpreted by the judge")
    category: str | None = Field(
        description="A category this feedback pertains to, if any"
    )
    judge_verdict: str | None = Field(
        description="The LLM judge's structured verdict, as JSON"
    )
    created_at: datetime = Field(description="When the feedback was recorded")


class IdentityOverride(FrozenModel):
    """A manual identity edit that always wins over automatic resolution (user-data layer)."""

    email: EmailAddress = Field(description="The email address being overridden")
    override_type: OverrideType = Field(description="Add-to-person or split-out")
    forced_person_id: PersonId | None = Field(
        description="Target person for an add override"
    )
    reason: str | None = Field(description="Why the user made this override")
    backfill_status: BackfillStatus = Field(description="On-demand backfill progress")
    backfilled_at: datetime | None = Field(
        description="When the backfill last completed"
    )
    created_at: datetime = Field(description="When the override was created")


class FilterRule(FrozenModel):
    """An exclusion rule for the CRM view (user-data layer)."""

    rule_type: FilterRuleType = Field(
        description="Whether this targets a domain or an email"
    )
    value: str = Field(description="The domain or email the rule matches")
    label: str | None = Field(description="A user-facing label for this rule")
    source: FilterRuleSource = Field(
        description="Whether this rule was seeded or user-added"
    )


# --- Configuration + AI ---


class MetricsConfig(FrozenModel):
    """Tunable rules for deriving metrics from the interaction log."""

    self_emails: tuple[EmailAddress, ...] = Field(
        description="Addresses that represent the user"
    )
    small_group_max_attendees: NonNegativeInt = Field(
        description="Max attendees for a focused meeting"
    )
    social_title_keywords: tuple[str, ...] = Field(
        description="Title keywords that mark a social event"
    )


class CategoryJudgeVerdict(FrozenModel):
    """The structured result of the LLM judge interpreting free-form category feedback."""

    suggested_categories: tuple[str, ...] = Field(
        description="Categories the judge inferred from the note"
    )
    interpretation: str = Field(
        description="A short natural-language interpretation of the note"
    )
    is_positive: bool = Field(
        description="Whether the judge read the sentiment as positive"
    )


# --- Ingest parse results (transient) ---


class ObservedName(FrozenModel):
    """A display name observed for an email address during ingestion, with its provenance."""

    email: EmailAddress = Field(description="The address the name was observed for")
    name: DisplayName = Field(description="The observed display name")
    source: NameSource = Field(description="Where the name was observed")


class ParsedInteraction(FrozenModel):
    """The result of parsing one raw Google record: an interaction plus any names it revealed."""

    interaction: Interaction = Field(description="The parsed interaction")
    observed_names: tuple[ObservedName, ...] = Field(
        description="Display names revealed by the record"
    )


class SyncResult(FrozenModel):
    """A summary of what a sync run ingested and derived."""

    calendar_interactions_ingested: NonNegativeInt = Field(
        description="Calendar events upserted this run"
    )
    email_interactions_ingested: NonNegativeInt = Field(
        description="Email messages upserted this run"
    )
    person_count: NonNegativeInt = Field(description="Total persons after resolution")
    org_count: NonNegativeInt = Field(description="Total orgs after resolution")
    metric_count: NonNegativeInt = Field(description="Persons with computed metrics")


class MigrationResult(FrozenModel):
    """A summary of what a legacy-JSON migration imported."""

    email_identities_seeded: NonNegativeInt = Field(
        description="Email identities seeded from the rollup"
    )
    filter_rules_seeded: NonNegativeInt = Field(
        description="Exclusion rules seeded from filters.json"
    )
    orgs_seeded: NonNegativeInt = Field(
        description="Orgs seeded from the seed domain map"
    )
    org_domains_seeded: NonNegativeInt = Field(
        description="Domain->org mappings seeded"
    )
