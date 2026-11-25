import pytest

from people_crm.ai_judge import _extract_json_object, _parse_verdict
from people_crm.data_types import FeedbackSignal
from people_crm.errors import CategoryJudgeError
from people_crm.mock_ai_judge_test import MockCategoryJudge


def test_parse_verdict_reads_categories_and_sentiment() -> None:
    content = '{"suggested_categories": ["investor", "advisor"], "interpretation": "helpful", "is_positive": true}'

    verdict = _parse_verdict(content, FeedbackSignal.POSITIVE)

    assert verdict.suggested_categories == ("investor", "advisor")
    assert verdict.interpretation == "helpful"
    assert verdict.is_positive is True


def test_parse_verdict_extracts_object_from_surrounding_text() -> None:
    content = 'Here you go:\n{"suggested_categories": ["friend"], "interpretation": "x", "is_positive": false}\nThanks'

    verdict = _parse_verdict(content, FeedbackSignal.NEGATIVE)

    assert verdict.suggested_categories == ("friend",)


def test_extract_json_object_raises_on_missing_object() -> None:
    with pytest.raises(CategoryJudgeError):
        _extract_json_object("no json here")


def test_mock_category_judge_records_calls_and_uses_signal() -> None:
    judge = MockCategoryJudge(fixed_categories=("mentor",))

    verdict = judge.judge_category_note("Dana", "great mentor", FeedbackSignal.POSITIVE)

    assert verdict.suggested_categories == ("mentor",)
    assert verdict.is_positive is True
    assert judge.call_count == 1
