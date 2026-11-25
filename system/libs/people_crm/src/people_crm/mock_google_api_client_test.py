from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import Field

from people_crm.errors import GoogleApiError
from people_crm.interfaces import GoogleApiClientInterface, RawJson


class MockGoogleApiClient(GoogleApiClientInterface):
    """In-memory Google API client that serves pre-configured raw records for tests."""

    calendar_events: tuple[RawJson, ...] = Field(
        default=(), description="Raw calendar events to serve"
    )
    gmail_message_ids: tuple[str, ...] = Field(
        default=(), description="Message ids returned by a search"
    )
    gmail_messages_by_id: Mapping[str, RawJson] = Field(
        default={}, description="Metadata keyed by message id"
    )
    failing_gmail_message_ids: frozenset[str] = Field(
        default=frozenset(),
        description="Message ids whose metadata fetch raises GoogleApiError (simulating a persistent fetch failure)",
    )
    contacts: tuple[RawJson, ...] = Field(
        default=(), description="Raw People API connections"
    )
    account_email: str = Field(
        default="me@example.com", description="Address of the connected account"
    )

    def fetch_calendar_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        search_query: str | None,
    ) -> list[RawJson]:
        if search_query is None:
            return list(self.calendar_events)
        # Emulate the free-text query: keep events whose attendees include the address.
        query = search_query.lower()
        return [
            event
            for event in self.calendar_events
            if any(
                query == (attendee.get("email") or "").lower()
                for attendee in event.get("attendees", [])
            )
        ]

    def fetch_gmail_message_ids(self, search_query: str, max_results: int) -> list[str]:
        return list(self.gmail_message_ids)[:max_results]

    def fetch_gmail_message_metadata(self, message_id: str) -> RawJson:
        if message_id in self.failing_gmail_message_ids:
            raise GoogleApiError(f"simulated fetch failure for {message_id}")
        return self.gmail_messages_by_id[message_id]

    def fetch_contacts(self) -> list[RawJson]:
        return list(self.contacts)

    def fetch_account_email(self) -> str:
        return self.account_email


def make_mock_google_api_client(
    calendar_events: Sequence[RawJson],
    gmail_message_ids: Sequence[str],
    gmail_messages_by_id: Mapping[str, RawJson],
    contacts: Sequence[RawJson],
) -> MockGoogleApiClient:
    return MockGoogleApiClient(
        calendar_events=tuple(calendar_events),
        gmail_message_ids=tuple(gmail_message_ids),
        gmail_messages_by_id=dict(gmail_messages_by_id),
        contacts=tuple(contacts),
    )
