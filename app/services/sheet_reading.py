import logging
from typing import Any

from app.services.ai_provider import (
    AIProviderError,
    image_content,
    output_text,
    parse_json_object,
    request_ai_provider,
)
from app.utils.options import (
    BENGALI_DIGITS,
    normalize_option_label,
    normalize_options,
)
from app.utils.student_keys import normalize_class_name

logger = logging.getLogger(__name__)
FIELD_ALIASES = {
    "roll_no": "roll",
    "registration_no": "registration",
    "class": "class_name",
    "শ্রেণি": "class_name",
    "শ্রেণী": "class_name",
}


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().translate(BENGALI_DIGITS).upper().split())


def normalize_code(value: Any) -> str:
    text = normalize_text(value)
    return normalize_option_label(text) or text


def read_answer_sheet(
    image,
    *,
    question_numbers: list[int],
    options_count: dict[int, int],
    model: str,
) -> dict[str, Any]:
    count = len(question_numbers)
    prompt = (
        f"This is a scanned Bangla MCQ answer sheet with {count} questions, numbered 1 to {count}. "
        "PART 1, answers: for each question list the label(s) of the FILLED bubble(s) exactly "
        "as printed above the column (for example ক, খ, গ, ঘ). Do not translate labels. Use [] "
        'when no bubble is filled. List faint, erased or unclear question numbers in "unclear". '
        "PART 2, the header box: copy the value written next to each label exactly as written, "
        "without translating or transliterating: নাম (name), রোল (roll), রেজিস্ট্রেশন "
        "(registration), শ্রেণি (class, for example নবম), শাখা (section, usually one letter "
        "such as ক) and সেট (set, usually one letter such as ক). Section and set are "
        "different fields. Use null for a field that is empty or absent. List a field's key in "
        '"unclear_fields" only if something is written there that you cannot read. Never guess. '
        'Return only JSON: {"answers":{"1":["ক"]},"unclear":[],"name":null,"roll":null,'
        '"registration":null,"class_name":null,"section":null,"set_code":null,'
        '"subject_code":null,"unclear_fields":[]}'
    )
    content = [{"type": "text", "text": prompt}, image_content(image)]
    response = request_ai_provider(content, model=model)
    payload = parse_json_object(output_text(response))
    logger.info(
        "Identity read: %s",
        {
            key: payload.get(key)
            for key in (
                "name",
                "roll",
                "registration",
                "class_name",
                "section",
                "set_code",
            )
        },
    )
    return validate_sheet_read(payload, question_numbers, options_count)


def validate_sheet_read(
    payload: dict[str, Any], question_numbers: list[int], options_count: dict[int, int]
) -> dict[str, Any]:
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, dict):
        raise AIProviderError("The AI response did not contain an answers object.")
    answers: dict[int, list[str]] = {}
    unclear_numbers: set[int] = set()
    for number in question_numbers:
        raw = raw_answers.get(str(number))
        if raw is None:
            answers[number] = []
            continue
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            answers[number] = []
            unclear_numbers.add(number)
            continue
        normalized, bad = normalize_options(raw)
        allowed = {chr(ord("A") + index) for index in range(options_count[number])}
        if bad or not set(normalized).issubset(allowed):
            answers[number] = []
            unclear_numbers.add(number)
        else:
            answers[number] = normalized
    raw_unclear = payload.get("unclear")
    if isinstance(raw_unclear, list):
        for item in raw_unclear:
            try:
                number = int(str(item).translate(BENGALI_DIGITS))
            except (TypeError, ValueError):
                continue
            if number in question_numbers:
                unclear_numbers.add(number)
    raw_fields = payload.get("unclear_fields")
    unclear_fields = {
        FIELD_ALIASES.get(str(field).strip().lower(), str(field).strip().lower())
        for field in (raw_fields if isinstance(raw_fields, list) else [])
    }
    name = payload.get("name")
    return {
        "answers": answers,
        "unclear": sorted(unclear_numbers),
        "unclear_fields": unclear_fields,
        "invalid": False,
        "name": name.strip() if isinstance(name, str) else "",
        "roll": normalize_text(payload.get("roll")),
        "registration": normalize_text(payload.get("registration")),
        "class_name": normalize_class_name(payload.get("class_name")),
        "subject_code": normalize_text(payload.get("subject_code")),
        "set_code": normalize_code(payload.get("set_code")),
        "section": normalize_text(payload.get("section")),
    }
