import base64
import json
import mimetypes
import re
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings

from filesystem.models.models import ImageModel

MAX_ANSWER_IMAGE_SIZE = 5 * 1024 * 1024


class AnswerExtractionError(Exception):
    def __init__(self, detail: str, *, status_code: int = 502):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _output_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AnswerExtractionError("The AI provider returned no answer.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AnswerExtractionError("The AI provider returned an invalid answer.")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "\n".join(text_parts)
    raise AnswerExtractionError("The AI provider returned an invalid answer.")


def _parse_answer_json(output_text: str) -> list[dict[str, Any]]:
    payload = _parse_json_object(output_text)
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise AnswerExtractionError("The AI response did not contain an answers list.")
    return answers


def _parse_json_object(output_text: str) -> dict[str, Any]:
    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise AnswerExtractionError(
                "The AI response did not contain valid JSON."
            ) from None
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AnswerExtractionError(
                "The AI response did not contain valid JSON."
            ) from exc

    if not isinstance(payload, dict):
        raise AnswerExtractionError("The AI response did not contain a JSON object.")
    return payload


def _image_content(image: ImageModel, *, max_size: int) -> tuple[dict[str, Any], int]:
    mime_type = mimetypes.guess_type(image.image.name)[0]
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise AnswerExtractionError(
            "The uploaded file is not a supported image.", status_code=400
        )
    try:
        with image.image.open("rb") as source:
            image_bytes = source.read(max_size + 1)
    except OSError as exc:
        raise AnswerExtractionError(
            "The uploaded image could not be read.", status_code=400
        ) from exc
    if len(image_bytes) > max_size:
        raise AnswerExtractionError(
            "An uploaded image exceeds the allowed size.", status_code=400
        )
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    image_url = f"data:{mime_type};base64,{encoded_image}"
    if urlparse(settings.OPENROUTER_BASE_URL).hostname in {
        "generativelanguage.googleapis.com",
        "openrouter.ai",
    }:
        image_url = {"url": image_url}
    return (
        {
            "type": "image_url",
            "image_url": image_url,
        },
        len(image_bytes),
    )


def _provider_error_message(response: requests.Response, api_key: str) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return None
    message = " ".join(message.split()).replace(api_key, "[redacted]")
    return message[:500] or None


def _request_ai_provider(content: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        raise AnswerExtractionError(
            "Image extraction is not configured. Set OPENROUTER_API_KEY.",
            status_code=503,
        )
    try:
        response = requests.post(
            f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": settings.OPENROUTER_ANSWER_MODEL,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=(5, 60),
        )
        response.raise_for_status()
        response_data = response.json()
    except requests.HTTPError as exc:
        status_code = (
            exc.response.status_code if exc.response is not None else "unknown"
        )
        provider_message = (
            _provider_error_message(exc.response, api_key)
            if exc.response is not None
            else None
        )
        detail = (
            f"The AI provider rejected the request (HTTP {status_code}). "
            "Check the configured model, API key, and request format."
        )
        if provider_message:
            detail = f"{detail} Provider message: {provider_message}"
        raise AnswerExtractionError(detail) from exc
    except requests.RequestException as exc:
        raise AnswerExtractionError(
            "Could not connect to the configured AI provider. Check its base URL "
            "and network connection."
        ) from exc
    except ValueError as exc:
        raise AnswerExtractionError(
            "The AI provider returned an invalid response."
        ) from exc
    if not isinstance(response_data, dict):
        raise AnswerExtractionError("The AI provider returned an invalid response.")
    return response_data


def extract_answers_from_image(image: ImageModel) -> list[dict[str, Any]]:
    prompt = (
        "Read the complete answer key in this image and extract every visible "
        "question-number to answer-option mapping. The sheet may have multiple "
        "columns; inspect each column from top to bottom. Rows may contain "
        "explanations after a dash; use the answer option immediately following "
        "the question number and ignore the explanation. The image may use "
        "Bengali numerals or option labels. Convert Bengali digits "
        "০১২৩৪৫৬৭৮৯ to 0123456789. Map Bengali option labels ক to A, খ to B, "
        "গ to C, ঘ to D, ঙ to E, চ to F, ছ to G, জ to H, ঝ to I, and ঞ to J. "
        "Do not limit the result to any externally supplied question list. "
        "Transcribe every clearly readable mapping; do not guess unreadable "
        "entries. Return only JSON with this shape: "
        '{"answers":[{"question_number":1,"correct_options":["B"]}]}. '
        "Use one answer option per question unless the image clearly shows "
        "multiple correct options. Include all question numbers found in the "
        "image."
    )
    image_content, _ = _image_content(image, max_size=MAX_ANSWER_IMAGE_SIZE)
    response_data = _request_ai_provider(
        [{"type": "text", "text": prompt}, image_content]
    )
    return _parse_answer_json(_output_text(response_data))
