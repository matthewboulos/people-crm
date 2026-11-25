import subprocess

import pytest

from people_crm.errors import GoogleApiError
from people_crm.google_api import LatchkeyGoogleApiClient


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["latchkey", "curl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _ScriptedRunner:
    """Returns/raises a pre-scripted sequence of results across successive calls."""

    def __init__(self, results: list[object]) -> None:
        self._results = results
        self.calls = 0

    def __call__(self, argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        result = self._results[self.calls]
        self.calls += 1
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, subprocess.CompletedProcess)
        return result


def _client(runner: _ScriptedRunner) -> LatchkeyGoogleApiClient:
    # no-op backoff so the test never actually sleeps
    return LatchkeyGoogleApiClient(curl_runner=runner, backoff_sleep=lambda _seconds: None)


def test_retries_transient_curl_failure_then_succeeds() -> None:
    runner = _ScriptedRunner(
        [
            _completed(52),  # empty reply -- transient
            _completed(7),  # connection refused -- transient
            _completed(0, stdout='{"ok": true}'),
        ]
    )
    client = _client(runner)

    assert client._get_json("https://example.test/x") == {"ok": True}
    assert runner.calls == 3


def test_retries_timeout_then_succeeds() -> None:
    runner = _ScriptedRunner(
        [
            subprocess.TimeoutExpired(cmd="latchkey curl", timeout=1.0),
            _completed(0, stdout='{"ok": true}'),
        ]
    )
    client = _client(runner)

    assert client._get_json("https://example.test/x") == {"ok": True}
    assert runner.calls == 2


def test_non_transient_failure_raises_immediately_without_retry() -> None:
    runner = _ScriptedRunner([_completed(1, stderr="bad request")])
    client = _client(runner)

    with pytest.raises(GoogleApiError):
        client._get_json("https://example.test/x")
    assert runner.calls == 1


def test_persistent_transient_failure_gives_up_after_max_attempts() -> None:
    runner = _ScriptedRunner([_completed(52) for _ in range(4)])
    client = _client(runner)

    with pytest.raises(GoogleApiError):
        client._get_json("https://example.test/x")
    assert runner.calls == 4


def test_account_email_comes_from_the_gmail_profile() -> None:
    runner = _ScriptedRunner([_completed(0, stdout='{"emailAddress": "me@example.com"}')])

    assert _client(runner).fetch_account_email() == "me@example.com"


def test_account_email_missing_from_profile_raises() -> None:
    runner = _ScriptedRunner([_completed(0, stdout="{}")])

    with pytest.raises(GoogleApiError):
        _client(runner).fetch_account_email()
