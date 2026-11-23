from typing import Any

from app.services.ai_provider import (
    AIProviderError,
    image_content,
    output_text,
    parse_json_object,
    request_ai_provider,
    validate_sheet_image,
)
from filesystem.models.models import ImageModel


def extract_answers_from_image(image: ImageModel) -> list[dict[str, Any]]:
    prompt = (
        "Read the complete answer key in this image and extract every visible "
        "question-number to answer-option mapping. The sheet may have multiple "
        "columns; inspect each column from top to bottom. Rows may contain "
        "explanations after a dash; use the answer option immediately following "
        "the question number and ignore the explanation. Entries and option "
        "letters may be handwritten, including handwritten Bengali. Read the "
        "visible option glyph for each row independently; never infer answers "
        "from a sequence, neighboring rows, or the explanation text. If an "
        "entry is unreadable, do not guess it. The image may use Bengali "
        "numerals or option labels. Return every option label exactly as written "
        "(for example ক or A); do not translate it. "
        "Do not limit the result to any externally supplied question list. "
        "Transcribe every clearly readable mapping; do not guess unreadable "
        "entries. Return only JSON with this shape: "
        '{"answers":[{"question_number":1,"correct_options":["B"]}]}. '
        "Use one answer option per question unless the image clearly shows "
        "multiple correct options. Include all question numbers found in the "
        "image."
    )
    validate_sheet_image(image)
    response_data = request_ai_provider(
        [{"type": "text", "text": prompt}, image_content(image)]
    )
    answers = parse_json_object(output_text(response_data)).get("answers")
    if not isinstance(answers, list):
        raise AIProviderError("The AI response did not contain an answers list.")
    return answers
