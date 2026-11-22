import base64
import json
import re
import warnings
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
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


FREE_VISION_MODELS_CACHE_KEY = "openrouter_free_vision_models"


def validate_sheet_reading_config() -> None:
    if not settings.OPENROUTER_API_KEY.strip():
        raise ConfigurationError(
            "Sheet reading is not configured. Set OPENROUTER_API_KEY."
        )


def free_vision_models(*, refresh: bool = False) -> list[str]:
    if refresh:
        cache.delete(FREE_VISION_MODELS_CACHE_KEY)
    else:
        cached_models = cache.get(FREE_VISION_MODELS_CACHE_KEY)
        if isinstance(cached_models, list) and all(
            isinstance(model, str) for model in cached_models
        ):
            return cached_models

    try:
        response = requests.get(
            f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            timeout=(5, 20),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 429 or (status_code is not None and status_code >= 500):
            raise ProviderTransientError(
                "The OpenRouter model list is temporarily unavailable.",
                status_code=status_code,
            ) from exc
        raise AIProviderError(
            "Could not retrieve the OpenRouter model list.",
            status_code=status_code or 502,
        ) from exc
    except requests.RequestException as exc:
        raise ProviderTransientError(
            "Could not connect to the OpenRouter model list."
        ) from exc
    except ValueError as exc:
        raise AIProviderError(
            "The OpenRouter model list returned invalid JSON."
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise AIProviderError("The OpenRouter model list has an invalid format.")

    models = set()
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        architecture = item.get("architecture")
        if (
            not isinstance(model_id, str)
            or not model_id.endswith(":free")
            or not isinstance(architecture, dict)
        ):
            continue

        input_modalities = architecture.get("input_modalities")
        takes_images = isinstance(input_modalities, list) and any(
            isinstance(modality, str) and modality.casefold() == "image"
            for modality in input_modalities
        )
        modality = architecture.get("modality")
        if isinstance(modality, str):
            input_format = modality.split("->", 1)[0]
            takes_images = takes_images or any(
                part.strip().casefold() == "image" for part in input_format.split("+")
            )
        if takes_images:
            models.add(model_id)

    if not models:
        raise ProviderTransientError(
            "No free vision-capable OpenRouter models are currently available."
        )

    available_models = sorted(models)
    cache.set(FREE_VISION_MODELS_CACHE_KEY, available_models, timeout=3600)
    return available_models


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
    return {"type": "image_url", "image_url": _wrap_url(url)}


def _wrap_url(url: str):
    host = urlparse(settings.OPENROUTER_BASE_URL).hostname
    return (
        {"url": url}
        if host
        in {
            "generativelanguage.googleapis.com",
            "openrouter.ai",
        }
        else url
    )


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
    return {"type": "image_url", "image_url": _wrap_url(url)}


def output_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AIProviderError("The AI provider returned no answer.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AIProviderError("The AI provider returned an invalid answer.")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    raise AIProviderError("The AI provider returned an invalid answer.")


def parse_json_object(output_text_value: str) -> dict[str, Any]:
    cleaned = output_text_value.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise AIProviderError("The AI provider returned invalid JSON.")
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AIProviderError("The AI provider returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise AIProviderError("The AI provider returned an invalid JSON object.")
    return payload


def request_ai_provider(content: list[dict[str, Any]], *, model: str) -> dict[str, Any]:
    validate_sheet_reading_config()
    api_key = settings.OPENROUTER_API_KEY
    try:
        response = requests.post(
            f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={"model": model, "messages": [{"role": "user", "content": content}]},
            timeout=(5, 60),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {404, 429} or (
            status_code is not None and status_code >= 500
        ):
            raise ProviderTransientError(
                "The AI provider is temporarily unavailable or the model is no longer available.",
                status_code=status_code,
            ) from exc
        raise AIProviderError(
            "The AI provider rejected the sheet-reading request.",
            status_code=status_code or 502,
        ) from exc
    except requests.RequestException as exc:
        raise ProviderTransientError(
            "Could not connect to the configured AI provider."
        ) from exc
    except ValueError as exc:
        raise AIProviderError("The AI provider returned an invalid response.") from exc
    if not isinstance(payload, dict):
        raise AIProviderError("The AI provider returned an invalid response.")
    return payload
