from pydantic import Field

from people_crm.data_types import CategoryJudgeVerdict, FeedbackSignal
from people_crm.interfaces import AiJudgeInterface


class MockCategoryJudge(AiJudgeInterface):
    """Deterministic category judge for tests: echoes a fixed verdict and records calls."""

    fixed_categories: tuple[str, ...] = Field(
        default=("test-category",), description="Categories to return"
    )
    call_count: int = Field(default=0, description="Number of judge calls made")

    def judge_category_note(
        self, person_display_name: str, note: str, signal: FeedbackSignal
    ) -> CategoryJudgeVerdict:
        self.call_count = self.call_count + 1
        return CategoryJudgeVerdict(
            suggested_categories=self.fixed_categories,
            interpretation=f"Note about {person_display_name}: {note}",
            is_positive=signal == FeedbackSignal.POSITIVE,
        )
