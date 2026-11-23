import base64
import json
import re
import warnings
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

import requests
from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

from filesystem.models.models import ImageModel


class AIProviderError(Exception):
    def __init__(self, detail: str, *, status_code: int = 502):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


class ConfigurationError(Exception):
    pass


class ProviderTransientError(AIProviderError):
    pass


class SheetImageError(Exception):
    pass


def validate_ai_provider_config() -> None:
    if not settings.COARENA_API_KEY.strip():
        raise ConfigurationError("AI is not configured. Set COARENA_API_KEY.")
    if not settings.COARENA_MODEL.strip():
        raise ConfigurationError("AI is not configured. Set COARENA_MODEL.")
    if settings.COARENA_MAX_TOKENS < 1:
        raise ConfigurationError("COARENA_MAX_TOKENS must be a positive integer.")


def validate_sheet_image(image: ImageModel) -> None:
    max_image_size = 20 * 1024 * 1024
    max_dimension = 10000
    max_pixels = 40_000_000
    if not image.image or not image.image.name:
        raise SheetImageError("The uploaded answer-sheet image is missing.")
    try:
        image_size = image.image.size
    except OSError as exc:
        raise SheetImageError(
            "The uploaded answer-sheet image could not be read."
        ) from exc
    if image_size > max_image_size:
        raise SheetImageError("Answer-sheet images must be 20 MB or smaller.")

    suffix = PurePosixPath(image.image.name.replace("\\", "/")).suffix.lower()
    valid_suffixes = {
        "JPEG": {".jpg", ".jpeg"},
        "PNG": {".png"},
        "WEBP": {".webp"},
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with image.image.open("rb") as source:
                with Image.open(source) as opened:
                    image_format = opened.format
                    width, height = opened.size
                    opened.verify()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
    ) as exc:
        raise SheetImageError(
            "The uploaded answer-sheet file is not a valid image."
        ) from exc

    if suffix not in valid_suffixes.get(image_format, set()):
        raise SheetImageError(
            "Answer-sheet image extensions must match the JPEG, PNG, or WebP format."
        )
    if max(width, height) > max_dimension or width * height > max_pixels:
        raise SheetImageError("Answer-sheet image dimensions are too large.")


def image_content(image: ImageModel, *, max_edge: int = 1600) -> dict[str, Any]:
    try:
        with image.image.open("rb") as source:
            with Image.open(source) as opened:
                opened = ImageOps.exif_transpose(opened).convert("RGB")
                opened.thumbnail((max_edge, max_edge))
                output = BytesIO()
                opened.save(output, format="JPEG", quality=88)
    except (OSError, ValueError) as exc:
        raise AIProviderError(
            "The uploaded image could not be read.", status_code=400
        ) from exc
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    url = f"data:image/jpeg;base64,{encoded}"
    return {"type": "image_url", "image_url": {"url": url}}


def array_content(gray, *, quality: int = 85) -> dict[str, Any]:
    import cv2

    ok, buffer = cv2.imencode(
        ".jpg",
        gray,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not ok:
        raise AIProviderError("The image could not be encoded.", status_code=400)
    encoded = base64.b64encode(buffer).decode("ascii")
    url = f"data:image/jpeg;base64,{encoded}"
    return {"type": "image_url", "image_url": {"url": url}}


def output_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AIProviderError("The AI provider returned no answer.")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AIProviderError("The AI provider returned an invalid answer.")
    if choice.get("finish_reason") == "length":
        raise AIProviderError(
            "The model ran out of output tokens before answering. "
            "Increase COARENA_MAX_TOKENS or use a model/effort with less reasoning."
        )
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            elif isinstance(item.get("json"), dict):
                return json.dumps(item["json"], ensure_ascii=False)
        if text_parts:
            return "\n".join(text_parts)
    parsed = message.get("parsed")
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise AIProviderError("The AI provider refused the request.")
    if message.get("tool_calls"):
        raise AIProviderError(
            "The AI provider returned tool calls instead of an answer."
        )
    content_type = "null" if content is None else type(content).__name__
    message_fields = ", ".join(sorted(key for key in message if isinstance(key, str)))
    finish_reason = choice.get("finish_reason")
    finish_reason = finish_reason if isinstance(finish_reason, str) else "unknown"
    raise AIProviderError(
        "The AI provider returned an unsupported answer format "
        f"(content type: {content_type}; message fields: {message_fields}; "
        f"finish reason: {finish_reason})."
    )


def parse_json_object(output_text_value: str) -> dict[str, Any]:
    cleaned = output_text_value.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        candidates = []
        for start, character in enumerate(cleaned):
            if character != "{":
                continue
            try:
                candidate, end = decoder.raw_decode(cleaned, start)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                candidates.append((end, candidate))
        if not candidates:
            raise AIProviderError("The AI provider returned invalid JSON.")
        payload = max(candidates, key=lambda item: item[0])[1]
    if not isinstance(payload, dict):
        raise AIProviderError("The AI provider returned an invalid JSON object.")
    return payload


def request_ai_provider(content: list[dict[str, Any]]) -> dict[str, Any]:
    validate_ai_provider_config()
    request_data: dict[str, Any] = {
        "model": settings.COARENA_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": settings.COARENA_MAX_TOKENS,
    }
    reasoning_effort = settings.COARENA_REASONING_EFFORT.strip()
    if reasoning_effort:
        request_data["reasoning_effort"] = reasoning_effort
    try:
        response = requests.post(
            f"{settings.COARENA_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.COARENA_API_KEY}",
            },
            json=request_data,
            timeout=(5, 120),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {401, 403}:
            raise AIProviderError(
                "The Coarena API credentials or permissions are invalid.",
                status_code=502,
            ) from exc
        detail = "The Coarena API rejected the request."
        if exc.response is not None:
            try:
                error_payload = exc.response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict):
                error = error_payload.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    detail = error["message"]
        if status_code in {404, 429} or (
            status_code is not None and status_code >= 500
        ):
            raise ProviderTransientError(
                detail,
                status_code=502 if status_code == 404 else status_code or 502,
            ) from exc
        raise AIProviderError(detail, status_code=502) from exc
    except requests.RequestException as exc:
        raise ProviderTransientError("Could not connect to the Coarena API.") from exc
    except ValueError as exc:
        raise AIProviderError("Coarena returned an invalid response.") from exc
    if not isinstance(payload, dict):
        raise AIProviderError("Coarena returned an invalid response.")
    return payload
