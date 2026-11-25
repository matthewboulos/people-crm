import pytest

from people_crm.primitives import DomainName, EmailAddress, PersonId


def test_email_address_lowercases_and_strips() -> None:
    email = EmailAddress("  Alice.Smith@Example.ORG ")

    assert email == "alice.smith@example.org"
    assert email.domain == "example.org"


def test_email_address_reports_free_email_domain() -> None:
    assert EmailAddress("someone@gmail.com").is_free_email is True
    assert EmailAddress("someone@example.org").is_free_email is False


@pytest.mark.parametrize(
    "invalid_value",
    ["", "no-at-symbol", "two@@ats.com", "missing@domain", "@nolocal.com"],
)
def test_email_address_rejects_malformed_values(invalid_value: str) -> None:
    with pytest.raises(ValueError):
        EmailAddress(invalid_value)


def test_domain_name_requires_a_dot() -> None:
    with pytest.raises(ValueError):
        DomainName("localhost")


def test_person_id_round_trips_through_prefix() -> None:
    generated = PersonId.generate()

    assert generated.startswith("per-")
    assert PersonId(str(generated)) == generated
