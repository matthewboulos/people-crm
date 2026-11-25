class PeopleCrmError(Exception):
    """Base exception for all people-crm errors."""

    ...


class CrmStoreError(PeopleCrmError):
    """Raised when a store operation fails."""

    ...


class PersonNotFoundError(CrmStoreError, KeyError):
    """Raised when a person id does not exist in the store."""

    def __init__(self, person_id: str) -> None:
        self.person_id = person_id
        super().__init__(f"Person '{person_id}' not found")


class GoogleApiError(PeopleCrmError):
    """Raised when a Google API call via latchkey fails or returns malformed data."""

    ...


class MigrationError(PeopleCrmError):
    """Raised when importing legacy JSON data fails."""

    ...


class CategoryJudgeError(PeopleCrmError):
    """Raised when the AI category judge call fails."""

    ...
