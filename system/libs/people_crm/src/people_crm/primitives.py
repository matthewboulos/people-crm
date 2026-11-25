from typing import Any, Self

from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.primitives import NonEmptyStr
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

# Email domains that belong to consumer mailbox providers rather than an
# organization. People on these domains are never auto-grouped into an org.
FREE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "fastmail.com",
        "hey.com",
        "msn.com",
        "gmx.com",
        "zoho.com",
    }
)


class InvalidEmailAddressError(ValueError):
    """Raised when an email address value is malformed."""


class InvalidDomainNameError(ValueError):
    """Raised when a domain name value is malformed."""


class PersonId(RandomId):
    """Unique identifier for a merged person entity."""

    PREFIX = "per"


class OrgId(RandomId):
    """Unique identifier for an organization entity."""

    PREFIX = "org"


class InteractionId(RandomId):
    """Unique identifier for a single ingested interaction (calendar event or email)."""

    PREFIX = "int"


class FeedbackId(RandomId):
    """Unique identifier for a single piece of category feedback."""

    PREFIX = "fbk"


class EmailAddress(str):
    """A lower-cased, non-empty email address containing exactly one '@'."""

    def __new__(cls, value: str) -> Self:
        normalized = value.strip().lower()
        if not normalized:
            raise InvalidEmailAddressError("Email address cannot be empty")
        if normalized.count("@") != 1:
            raise InvalidEmailAddressError(
                f"Email address must contain exactly one '@': {value!r}"
            )
        local, domain = normalized.split("@")
        if not local or not domain or "." not in domain:
            raise InvalidEmailAddressError(
                f"Email address is not well-formed: {value!r}"
            )
        return super().__new__(cls, normalized)

    @property
    def domain(self) -> "DomainName":
        return DomainName(self.split("@")[1])

    @property
    def is_free_email(self) -> bool:
        return self.split("@")[1] in FREE_EMAIL_DOMAINS

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class DomainName(str):
    """A lower-cased, non-empty DNS domain (the part after '@')."""

    def __new__(cls, value: str) -> Self:
        normalized = value.strip().lower()
        if not normalized or "." not in normalized:
            raise InvalidDomainNameError(f"Domain name is not well-formed: {value!r}")
        return super().__new__(cls, normalized)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class DisplayName(NonEmptyStr):
    """A human display name for a person or org."""

    ...


class OrgName(NonEmptyStr):
    """The name of an organization."""

    ...


class SourceRef(NonEmptyStr):
    """A source system's stable id for a record (Google eventId / Gmail messageId)."""

    ...
