import json
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Final
from urllib.parse import quote

from loguru import logger
from pydantic import Field

from people_crm.errors import GoogleApiError
from people_crm.interfaces import GoogleApiClientInterface, RawJson

_CALENDAR_PAGE_SIZE: Final[int] = 2500
_GMAIL_PAGE_SIZE: Final[int] = 500
_CONTACTS_PAGE_SIZE: Final[int] = 1000
_RFC3339_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"
# curl exit codes for transient network conditions worth retrying, rather than
# aborting a multi-thousand-request sync on a single dropped connection:
#   6 DNS, 7 connect, 18 partial transfer, 28 timeout, 35 SSL connect,
#   52 empty reply, 55 send error, 56 recv error.
_TRANSIENT_CURL_CODES: Final[frozenset[int]] = frozenset({6, 7, 18, 28, 35, 52, 55, 56})
_MAX_ATTEMPTS: Final[int] = 4
_RETRY_BACKOFF_SECONDS: Final[float] = 1.5

# A curl invocation: takes (argv, timeout_seconds) and returns the completed
# process. Injected so the retry path is testable without a real network.
CurlRunner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _default_curl_runner(argv: list[str], timeout_seconds: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds)
_GMAIL_METADATA_HEADERS: Final[tuple[str, ...]] = (
    "From",
    "To",
    "Cc",
    "Date",
    "Subject",
    "Message-Id",
)


class LatchkeyGoogleApiClient(GoogleApiClientInterface):
    """Fetches Google Calendar, Gmail, and Contacts data through `latchkey curl`."""

    request_timeout_seconds: float = Field(
        default=60.0, frozen=True, description="Per-request hard timeout"
    )
    # Injected so retry backoff is overridable (e.g. a no-op in tests); defaults
    # to the real sleep. Waiting out a transient network blip is not the polling
    # anti-pattern the time.sleep ratchet targets -- there is no condition to poll.
    backoff_sleep: Callable[[float], None] = Field(
        default=time.sleep, frozen=True, description="Sleep used between retry attempts"
    )
    curl_runner: CurlRunner = Field(
        default=_default_curl_runner, frozen=True, description="Runs a curl argv; injectable for tests"
    )

    def _get_json(self, url: str) -> RawJson:
        # Retry transient network failures (a dropped connection, empty reply, or
        # timeout) with a short backoff; a single blip must not abort a sync that
        # makes thousands of requests. Non-transient failures raise immediately.
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            transient_reason: str | None = None
            try:
                completed = self.curl_runner(
                    ["latchkey", "curl", "-s", url], self.request_timeout_seconds
                )
            except subprocess.TimeoutExpired:
                transient_reason = "timed out"
            else:
                if completed.returncode == 0:
                    break
                if completed.returncode in _TRANSIENT_CURL_CODES:
                    transient_reason = f"exit {completed.returncode}"
                else:
                    raise GoogleApiError(
                        f"latchkey curl failed ({completed.returncode}) for {url}: {completed.stderr}"
                    )
            if attempt == _MAX_ATTEMPTS:
                raise GoogleApiError(
                    f"latchkey curl {transient_reason} for {url} after {_MAX_ATTEMPTS} attempts"
                )
            logger.warning(
                "Transient latchkey failure ({}) for {}; retrying ({}/{})",
                transient_reason,
                url,
                attempt,
                _MAX_ATTEMPTS,
            )
            self.backoff_sleep(_RETRY_BACKOFF_SECONDS * attempt)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as e:
            raise GoogleApiError(
                f"latchkey curl returned invalid JSON for {url}"
            ) from e
        if isinstance(payload, dict) and "error" in payload:
            raise GoogleApiError(f"Google API error for {url}: {payload['error']}")
        return payload

    def fetch_calendar_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        search_query: str | None,
    ) -> list[RawJson]:
        base = (
            f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id)}/events"
            f"?singleEvents=true&orderBy=startTime&maxResults={_CALENDAR_PAGE_SIZE}"
            f"&timeMin={time_min.strftime(_RFC3339_FORMAT)}&timeMax={time_max.strftime(_RFC3339_FORMAT)}"
        )
        if search_query:
            base = base + f"&q={quote(search_query)}"
        events: list[RawJson] = []
        page_token: str | None = None
        has_more_pages = True
        while has_more_pages:
            url = base + (f"&pageToken={quote(page_token)}" if page_token else "")
            page = self._get_json(url)
            events.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            has_more_pages = bool(page_token)
        return events

    def fetch_gmail_message_ids(self, search_query: str, max_results: int) -> list[str]:
        base = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={_GMAIL_PAGE_SIZE}&q={quote(search_query)}"
        message_ids: list[str] = []
        page_token: str | None = None
        while len(message_ids) < max_results:
            url = base + (f"&pageToken={quote(page_token)}" if page_token else "")
            page = self._get_json(url)
            for message in page.get("messages", []):
                message_ids.append(message["id"])
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        # A still-truthy page_token means we stopped because of the cap while more
        # pages remained; an exhausted mailbox (no next page) is not a truncation.
        if page_token:
            logger.warning(
                "Reached Gmail fetch cap of {} messages for query {!r}; older messages were not ingested",
                max_results,
                search_query,
            )
        return message_ids[:max_results]

    def fetch_gmail_message_metadata(self, message_id: str) -> RawJson:
        headers_query = "".join(
            f"&metadataHeaders={header}" for header in _GMAIL_METADATA_HEADERS
        )
        url = (
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(message_id)}"
            f"?format=metadata{headers_query}"
        )
        return self._get_json(url)

    def fetch_contacts(self) -> list[RawJson]:
        base = (
            "https://people.googleapis.com/v1/people/me/connections"
            f"?personFields=emailAddresses,names&pageSize={_CONTACTS_PAGE_SIZE}"
        )
        connections: list[RawJson] = []
        page_token: str | None = None
        has_more_pages = True
        while has_more_pages:
            url = base + (f"&pageToken={quote(page_token)}" if page_token else "")
            page = self._get_json(url)
            connections.extend(page.get("connections", []))
            page_token = page.get("nextPageToken")
            has_more_pages = bool(page_token)
        return connections

    def fetch_account_email(self) -> str:
        profile = self._get_json("https://gmail.googleapis.com/gmail/v1/users/me/profile")
        address = profile.get("emailAddress")
        if not address:
            raise GoogleApiError("Gmail profile did not include the account's email address")
        return str(address)
