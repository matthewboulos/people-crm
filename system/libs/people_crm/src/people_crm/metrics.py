from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Final

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.pure import pure

from people_crm.data_types import (
    Interaction,
    InteractionType,
    MetricsConfig,
    ParticipantRole,
    PersonMetric,
    ResponseStatus,
)
from people_crm.ingest import SOCIAL_TITLE_KEYWORDS
from people_crm.primitives import EmailAddress, PersonId

# The user's own addresses. None are hardcoded: the sync adds the connected
# Google account's address (see `metrics_config_for_account`), and calendar
# events mark the user's own attendee entry themselves.
DEFAULT_SELF_EMAILS: Final[tuple[EmailAddress, ...]] = ()

DEFAULT_SMALL_GROUP_MAX_ATTENDEES: Final[int] = 4

DEFAULT_METRICS_CONFIG: Final[MetricsConfig] = MetricsConfig(
    self_emails=DEFAULT_SELF_EMAILS,
    small_group_max_attendees=NonNegativeInt(DEFAULT_SMALL_GROUP_MAX_ATTENDEES),
    social_title_keywords=SOCIAL_TITLE_KEYWORDS,
)


@pure
def metrics_config_for_account(
    config: MetricsConfig, account_email: EmailAddress
) -> MetricsConfig:
    """Return the config with the connected account's address counted as the user."""
    if account_email in config.self_emails:
        return config
    return config.model_copy(
        update={"self_emails": (*config.self_emails, account_email)}
    )


@pure
def is_social_event(
    title: str, attendee_count: int, social_keywords: Sequence[str]
) -> bool:
    # Social detection only applies to groups (>2 people); a genuine 1:1 "coffee"
    # or "lunch" is still a real meeting.
    if attendee_count <= 2:
        return False
    padded_title = " " + title.lower() + " "
    return any(keyword in padded_title for keyword in social_keywords)


class _PersonAccumulator:
    """Mutable per-person tally used only within a single metric computation."""

    __slots__ = (
        "meeting_count",
        "one_on_one_count",
        "focused_count",
        "minutes_total",
        "minutes_focused",
        "first_date",
        "last_date",
        "counted_series",
    )

    def __init__(self) -> None:
        self.meeting_count = 0
        self.one_on_one_count = 0
        self.focused_count = 0
        self.minutes_total = 0
        self.minutes_focused = 0
        self.first_date: date | None = None
        self.last_date: date | None = None
        self.counted_series: set[str] = set()


@pure
def compute_person_metrics(
    interactions: Sequence[Interaction],
    email_to_person_id: Mapping[EmailAddress, PersonId],
    config: MetricsConfig,
    today: date,
    computed_at: datetime,
) -> list[PersonMetric]:
    """Derive per-person meeting metrics from the calendar interaction log (reproduces the prototype rules)."""
    self_email_set = {str(e) for e in config.self_emails}

    # Only timed calendar events participate; process earliest-first so the
    # single counted occurrence of a recurring series is deterministic.
    calendar_interactions = sorted(
        (
            interaction
            for interaction in interactions
            if interaction.interaction_type == InteractionType.CALENDAR
            and interaction.duration_minutes is not None
        ),
        key=lambda interaction: interaction.occurred_at,
    )

    accumulator_by_person: dict[PersonId, _PersonAccumulator] = {}
    for interaction in calendar_interactions:
        # Skip events the user explicitly declined.
        is_declined_by_user = any(
            participant.role == ParticipantRole.SELF
            and participant.response_status == ResponseStatus.DECLINED
            for participant in interaction.participants
        )
        if is_declined_by_user:
            continue

        # Distinct other people (mapping each email to its merged person).
        other_person_ids: list[PersonId] = []
        seen_person_ids: set[PersonId] = set()
        for participant in interaction.participants:
            if (
                participant.role == ParticipantRole.SELF
                or str(participant.email) in self_email_set
            ):
                continue
            person_id = email_to_person_id.get(participant.email)
            if person_id is None or person_id in seen_person_ids:
                continue
            seen_person_ids.add(person_id)
            other_person_ids.append(person_id)

        if not other_person_ids:
            continue

        attendee_count = len(other_person_ids) + 1
        is_one_on_one = len(other_person_ids) == 1
        is_social = is_social_event(
            interaction.title, attendee_count, config.social_title_keywords
        )
        is_focused = (
            attendee_count <= config.small_group_max_attendees and not is_social
        )
        occurrence_date = interaction.occurred_at.date()
        duration = (
            int(interaction.duration_minutes)
            if interaction.duration_minutes is not None
            else 0
        )
        series = interaction.recurring_series_ref

        for person_id in other_person_ids:
            accumulator = accumulator_by_person.setdefault(
                person_id, _PersonAccumulator()
            )
            if (
                accumulator.first_date is None
                or occurrence_date < accumulator.first_date
            ):
                accumulator.first_date = occurrence_date
            if accumulator.last_date is None or occurrence_date > accumulator.last_date:
                accumulator.last_date = occurrence_date

            # 1:1s and one-off group meetings count every occurrence; recurring
            # group meetings count once per person for the whole series.
            if is_one_on_one or not series:
                counts_now = True
            elif series in accumulator.counted_series:
                counts_now = False
            else:
                accumulator.counted_series.add(series)
                counts_now = True

            if counts_now:
                accumulator.meeting_count += 1
                accumulator.minutes_total += duration
                if is_one_on_one:
                    accumulator.one_on_one_count += 1
                if is_focused:
                    accumulator.focused_count += 1
                    accumulator.minutes_focused += duration

    return [
        _accumulator_to_metric(person_id, accumulator, today, computed_at)
        for person_id, accumulator in accumulator_by_person.items()
    ]


@pure
def _accumulator_to_metric(
    person_id: PersonId,
    accumulator: _PersonAccumulator,
    today: date,
    computed_at: datetime,
) -> PersonMetric:
    days_since_last = (
        (today - accumulator.last_date).days
        if accumulator.last_date is not None
        else None
    )
    return PersonMetric(
        person_id=person_id,
        meeting_count=NonNegativeInt(accumulator.meeting_count),
        one_on_one_count=NonNegativeInt(accumulator.one_on_one_count),
        focused_count=NonNegativeInt(accumulator.focused_count),
        minutes_total=NonNegativeInt(accumulator.minutes_total),
        minutes_focused=NonNegativeInt(accumulator.minutes_focused),
        first_interaction_date=accumulator.first_date,
        last_interaction_date=accumulator.last_date,
        days_since_last=NonNegativeInt(days_since_last)
        if days_since_last is not None and days_since_last >= 0
        else None,
        is_group_only=accumulator.focused_count == 0,
        computed_at=computed_at,
    )
