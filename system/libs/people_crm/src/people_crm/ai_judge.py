import json
from typing import Final

from litellm import ModelResponse, completion
from openai import OpenAIError
from pydantic import Field

from people_crm.data_types import CategoryJudgeVerdict, FeedbackSignal
from people_crm.errors import CategoryJudgeError
from people_crm.interfaces import AiJudgeInterface

_SYSTEM_PROMPT: Final[str] = (
    "You interpret a user's free-form feedback about a person in their personal CRM. "
    "Infer a small set of concise, lower-case category tags the note implies (e.g. 'investor', "
    "'close friend', 'former colleague'). Do not invent a rigid taxonomy; derive tags from the note. "
    "Respond with ONLY a JSON object with keys: "
    '"suggested_categories" (array of short strings), "interpretation" (one short sentence), '
    'and "is_positive" (boolean sentiment of the note).'
)


class LiteLlmCategoryJudge(AiJudgeInterface):
    """Interprets free-form category feedback using a keyed litellm completion call."""

    model: str = Field(
        default="claude-haiku-4-5", frozen=True, description="litellm model id to call"
    )

    def judge_category_note(
        self, person_display_name: str, note: str, signal: FeedbackSignal
    ) -> CategoryJudgeVerdict:
        user_prompt = (
            f"Person: {person_display_name}\n"
            f"Binary signal: {signal.value}\n"
            f"Free-form note: {note}"
        )
        try:
            response = completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except OpenAIError as e:
            raise CategoryJudgeError(
                f"litellm completion failed for model {self.model}"
            ) from e
        if not isinstance(response, ModelResponse):
            raise CategoryJudgeError("Expected a non-streaming completion response")
        content = response.choices[0].message.content
        if not content:
            raise CategoryJudgeError("litellm returned an empty completion")
        return _parse_verdict(content, signal)


def _parse_verdict(content: str, signal: FeedbackSignal) -> CategoryJudgeVerdict:
    payload = _extract_json_object(content)
    raw_categories = payload.get("suggested_categories", [])
    categories = tuple(str(item) for item in raw_categories if str(item).strip())
    interpretation = str(payload.get("interpretation", "")).strip()
    is_positive = bool(payload.get("is_positive", signal == FeedbackSignal.POSITIVE))
    return CategoryJudgeVerdict(
        suggested_categories=categories,
        interpretation=interpretation,
        is_positive=is_positive,
    )


def _extract_json_object(content: str) -> dict:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise CategoryJudgeError(f"No JSON object found in judge response: {content!r}")
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError as e:
        raise CategoryJudgeError(f"Invalid JSON in judge response: {content!r}") from e
    if not isinstance(parsed, dict):
        raise CategoryJudgeError(f"Judge response was not a JSON object: {content!r}")
    return parsed
