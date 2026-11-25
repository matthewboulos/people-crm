from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from imbue.imbue_common.mutable_model import MutableModel

from people_crm.data_types import (
    BackfillStatus,
    CategoryFeedback,
    CategoryJudgeVerdict,
    EmailIdentity,
    FeedbackSignal,
    FilterRule,
    IdentityOverride,
    Interaction,
    Org,
    OrgDomain,
    Person,
    PersonEmailLink,
    PersonMetric,
    PersonUserData,
    SourceSystem,
)
from people_crm.primitives import EmailAddress, PersonId

# Raw decoded JSON returned from a Google API. Its keys are genuinely dynamic
# (they come from the external service), so a dict is the right representation
# at this boundary; ingest.py parses it into typed data_types.
RawJson = dict[str, Any]


class CrmStoreInterface(MutableModel, ABC):
    """Persists and retrieves the three-layer CRM data model."""

    @abstractmethod
    def initialize(self) -> None:
        """Create the schema if it does not exist and open the connection."""

    @abstractmethod
    def close(self) -> None:
        """Close the underlying connection."""

    # --- Source layer ---

    @abstractmethod
    def upsert_email_identity(self, identity: EmailIdentity) -> None:
        """Insert or update an email identity, widening its first/last-seen window."""

    @abstractmethod
    def upsert_interaction(self, interaction: Interaction) -> None:
        """Insert or update an interaction and its participants, keyed by source ref."""

    @abstractmethod
    def get_interaction_by_source_ref(
        self, source_system: SourceSystem, source_ref: str
    ) -> Interaction | None:
        """Return the interaction with the given source ref, or None."""

    @abstractmethod
    def get_all_interactions(self) -> list[Interaction]:
        """Return every ingested interaction with its participants."""

    @abstractmethod
    def get_all_email_identities(self) -> list[EmailIdentity]:
        """Return every observed email identity."""

    # --- Derived layer ---

    @abstractmethod
    def replace_person_graph(
        self, persons: Sequence[Person], links: Sequence[PersonEmailLink]
    ) -> None:
        """Atomically replace the person and email->person tables."""

    @abstractmethod
    def get_all_persons(self) -> list[Person]:
        """Return every person."""

    @abstractmethod
    def get_all_person_email_links(self) -> list[PersonEmailLink]:
        """Return the full email->person mapping."""

    @abstractmethod
    def get_person_ids_with_user_data(self) -> set[PersonId]:
        """Return the ids of persons that carry any user data (notes, tier, tags, or feedback)."""

    @abstractmethod
    def upsert_org(self, org: Org) -> None:
        """Insert or update an org."""

    @abstractmethod
    def upsert_org_domain(self, org_domain: OrgDomain) -> None:
        """Insert or update a domain->org mapping."""

    @abstractmethod
    def get_all_orgs(self) -> list[Org]:
        """Return every org."""

    @abstractmethod
    def get_all_org_domains(self) -> list[OrgDomain]:
        """Return every domain->org mapping."""

    @abstractmethod
    def replace_person_metrics(self, metrics: Sequence[PersonMetric]) -> None:
        """Atomically replace the derived per-person metrics."""

    @abstractmethod
    def get_all_person_metrics(self) -> list[PersonMetric]:
        """Return every person's derived metrics."""

    # --- User-data layer (never written by sync) ---

    @abstractmethod
    def get_all_identity_overrides(self) -> list[IdentityOverride]:
        """Return every manual identity override."""

    @abstractmethod
    def upsert_identity_override(self, override: IdentityOverride) -> None:
        """Insert or update a manual identity override."""

    @abstractmethod
    def set_override_backfill_status(
        self,
        email: EmailAddress,
        status: BackfillStatus,
        backfilled_at: datetime | None,
    ) -> None:
        """Update the backfill progress of an add-email override."""

    @abstractmethod
    def get_all_filter_rules(self) -> list[FilterRule]:
        """Return every exclusion rule."""

    @abstractmethod
    def upsert_filter_rule(self, rule: FilterRule) -> None:
        """Insert or update an exclusion rule."""

    @abstractmethod
    def get_person_user_data(self, person_id: PersonId) -> PersonUserData | None:
        """Return the user-owned data for a person, or None."""

    @abstractmethod
    def upsert_person_user_data(self, user_data: PersonUserData) -> None:
        """Insert or update the user-owned data for a person."""

    @abstractmethod
    def get_tags_for_person(self, person_id: PersonId) -> list[str]:
        """Return the tags applied to a person."""

    @abstractmethod
    def add_person_tag(self, person_id: PersonId, tag: str) -> None:
        """Apply a tag to a person (idempotent)."""

    @abstractmethod
    def add_category_feedback(self, feedback: CategoryFeedback) -> None:
        """Record a piece of category feedback."""

    @abstractmethod
    def get_feedback_for_person(self, person_id: PersonId) -> list[CategoryFeedback]:
        """Return every feedback item recorded for a person."""


class GoogleApiClientInterface(MutableModel, ABC):
    """Fetches raw data from the user's Google Calendar, Gmail, and Contacts."""

    @abstractmethod
    def fetch_calendar_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        search_query: str | None,
    ) -> list[RawJson]:
        """Return raw calendar event objects in the window, optionally filtered by a free-text query."""

    @abstractmethod
    def fetch_gmail_message_ids(self, search_query: str, max_results: int) -> list[str]:
        """Return Gmail message ids matching the query, up to max_results."""

    @abstractmethod
    def fetch_gmail_message_metadata(self, message_id: str) -> RawJson:
        """Return a single Gmail message with metadata-format headers only."""

    @abstractmethod
    def fetch_contacts(self) -> list[RawJson]:
        """Return raw People API connection objects (each may carry several email addresses)."""

    @abstractmethod
    def fetch_account_email(self) -> str:
        """Return the email address of the connected Google account (the user themselves)."""


class AiJudgeInterface(MutableModel, ABC):
    """Interprets free-form category feedback into a structured verdict."""

    @abstractmethod
    def judge_category_note(
        self, person_display_name: str, note: str, signal: FeedbackSignal
    ) -> CategoryJudgeVerdict:
        """Interpret a free-form feedback note about a person into suggested categories."""
