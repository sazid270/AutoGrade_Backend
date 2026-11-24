from typing import Any

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.ai_provider import (
    AIProviderError,
    array_content,
    output_text,
    parse_json_object,
    request_ai_provider,
)
from app.utils import omr
from app.utils.options import BENGALI_DIGITS

ROLE_ALIASES = {
    "answer": "answers",
    "answers": "answers",
    "roll": "roll",
    "roll_no": "roll",
    "roll_number": "roll",
    "registration": "registration",
    "registration_no": "registration",
    "registration_number": "registration",
    "reg": "registration",
}


class TemplateMismatch(Exception):
    """The sheet does not fit its saved OMR template."""


class TemplateExtractionError(Exception):
    """A sheet image cannot produce a verifiable OMR template."""


def load_gray(image) -> np.ndarray:
    try:
        with image.image.open("rb") as source:
            with Image.open(source) as opened:
                opened = ImageOps.exif_transpose(opened).convert("L")
                gray = np.asarray(opened, dtype=np.uint8)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise TemplateMismatch("The image could not be decoded.") from exc
    if not gray.size:
        raise TemplateMismatch("The image could not be decoded.")
    return gray


def _label_components(aligned: np.ndarray, components: list[np.ndarray]) -> dict:
    candidates = [
        {
            "grid_id": index,
            "bounds": [
                round(float(circles[:, 0].min()), 1),
                round(float(circles[:, 1].min()), 1),
                round(float(circles[:, 0].max()), 1),
                round(float(circles[:, 1].max()), 1),
            ],
        }
        for index, circles in enumerate(components, start=1)
    ]
    prompt = (
        "The image is a scanned OMR answer sheet. Computer-detected bubble groups "
        "are outlined in red and labelled GRID 1, GRID 2, and so on. Classify each "
        "group. Do not count anything and do not estimate coordinates. Roles: "
        '"answers" (rows of choice bubbles such as ক খ গ ঘ or A B C D beside question '
        'numbers), "roll" or "registration" (a grid of digit columns, ten bubbles '
        'per column), or "ignore". One logical grid can be split into several '
        "groups; give every fragment the same role. For answers groups add "
        '"numbering": "column" if question numbers continue down one column before '
        'moving to the next column, or "row" if they continue across the row first. '
        'For roll and registration groups add "digit_order": the ten digits printed '
        "in the bubbles of one digit column, from top to bottom. Use the printed "
        'heading above each digit grid: the grid under "রোল নম্বর" (Roll) has role '
        '"roll" and the grid under "রেজিস্ট্রেশন নম্বর" (Registration) has role '
        '"registration". Each of these roles belongs to at most one group. If a '
        "heading is absent, no group gets that role. Return exactly one "
        "JSON object and nothing else, in this shape: "
        '{"grids":[{"grid_id":1,"role":"roll","digit_order":'
        '["0","1","2","3","4","5","6","7","8","9"]},'
        '{"grid_id":2,"role":"answers","numbering":"column"}]}'
        f"\nDetected groups: {candidates}"
    )
    masked = omr.mask_to_components(aligned, components)
    image = omr.annotate_components(masked, components)
    response = request_ai_provider(
        [
            {"type": "text", "text": prompt},
            array_content(image, quality=90),
        ]
    )
    try:
        payload = parse_json_object(output_text(response))
    except AIProviderError as exc:
        raise TemplateExtractionError(
            f"The AI could not return a valid grid classification: {exc.detail}"
        ) from exc
    if not isinstance(payload.get("grids"), list):
        raise TemplateExtractionError("The AI did not return a grid classification.")
    return payload


def _digit_field(circles: np.ndarray, digit_order: list[str]) -> dict | None:
    digits = omr.digit_columns(circles)
    if digits is None:
        return None
    return {
        "digits": digits,
        "digit_order": digit_order,
        "required": True,
        "diameter": round(float(np.median(circles[:, 2])), 1),
    }


def extract_template(image) -> dict[str, Any]:
    aligned = omr.align_page(load_gray(image))
    if aligned is None:
        raise TemplateExtractionError("Four corner markers were not found.")
    aligned = omr.prepare_template_sheet(aligned)
    components = omr.bubble_components(aligned)
    if not components:
        raise TemplateExtractionError("No bubble grids were detected.")
    classification = _label_components(aligned, components)
    labels = classification["grids"]
    by_id = {}
    for label in labels:
        if not isinstance(label, dict):
            continue
        grid_id = label.get("grid_id")
        if (
            not isinstance(grid_id, int)
            or isinstance(grid_id, bool)
            or not 1 <= grid_id <= len(components)
            or grid_id in by_id
        ):
            raise TemplateExtractionError(
                "The AI returned an invalid bubble-grid identifier."
            )
        by_id[grid_id] = label
    if set(by_id) != set(range(1, len(components) + 1)):
        raise TemplateExtractionError("The AI did not classify every detected grid.")

    roles: dict[str, list[tuple[int, dict]]] = {
        "answers": [],
        "roll": [],
        "registration": [],
    }
    for grid_id, label in by_id.items():
        role = ROLE_ALIASES.get(
            str(label.get("role", "")).strip().lower().replace(" ", "_")
        )
        if role in roles:
            roles[role].append((grid_id, label))
    for field_name in ("roll", "registration"):
        if len(roles[field_name]) > 1:
            ids = ", ".join(str(grid_id) for grid_id, _ in roles[field_name])
            raise TemplateExtractionError(
                f"The AI labeled grids {ids} as {field_name}, but a sheet has "
                "only one. Try again or use a clearer reference sheet."
            )
    if not roles["answers"] or not roles["roll"]:
        summary = ", ".join(
            f"GRID {grid_id}={label.get('role')!r}"
            for grid_id, label in sorted(by_id.items())
        )
        raise TemplateExtractionError(
            "An answer grid and a roll digit grid are required. "
            f"The AI labeled: {summary}."
        )

    numbering_values = [label.get("numbering") for _, label in roles["answers"]]
    numberings = (
        set(numbering_values)
        if all(isinstance(value, str) for value in numbering_values)
        else set()
    )
    numbering = next(iter(numberings)) if len(numberings) == 1 else None
    if numbering not in {"column", "row"}:
        raise TemplateExtractionError("The answer-grid numbering is unclear.")
    geometry_diagnostics: list[str] = []
    answers = omr.template_for_components(
        aligned,
        [components[grid_id - 1] for grid_id, _ in roles["answers"]],
        diagnostics=geometry_diagnostics,
    )
    if answers is None:
        detail = (
            geometry_diagnostics[0]
            if geometry_diagnostics
            else "The detected bubbles do not form a supported grid."
        )
        raise TemplateExtractionError(
            f"The answer grid could not be verified geometrically: {detail}"
        )
    answer_circles = np.concatenate(
        [components[grid_id - 1] for grid_id, _ in roles["answers"]], axis=0
    )
    grids = {
        "answers": {
            "bounds": omr.grid_bounds(answer_circles),
            "columns": len({question["col"] for question in answers["questions"]}),
            "questions": len(answers["questions"]),
            "numbering": numbering,
        }
    }
    order = omr.question_order(answers, numbering)
    options_per_question = [len(answers["questions"][row]["bubbles"]) for row in order]
    if not options_per_question or any(
        not 2 <= count <= 10 for count in options_per_question
    ):
        raise TemplateExtractionError("The detected answer grid is incomplete.")
    option_labels_by_row = [
        [chr(65 + index) for index in range(len(question["bubbles"]))]
        for question in answers["questions"]
    ]

    fields = {}
    for field_name in ("roll", "registration"):
        if not roles[field_name]:
            if field_name == "roll":
                raise TemplateExtractionError("A roll-number digit grid is required.")
            continue
        digit_orders = set()
        circles = []
        for field_id, field_label in roles[field_name]:
            raw_digit_order = field_label.get("digit_order")
            digit_order = (
                tuple(str(value).translate(BENGALI_DIGITS) for value in raw_digit_order)
                if isinstance(raw_digit_order, list)
                else ()
            )
            if len(digit_order) != 10 or set(digit_order) != set("0123456789"):
                raise TemplateExtractionError(
                    f"The {field_name} digit order is unclear."
                )
            digit_orders.add(digit_order)
            circles.append(components[field_id - 1])
        digit_order = list(next(iter(digit_orders))) if len(digit_orders) == 1 else []
        if len(digit_order) != 10 or set(digit_order) != set("0123456789"):
            raise TemplateExtractionError(f"The {field_name} digit order is unclear.")
        field = _digit_field(np.concatenate(circles, axis=0), digit_order)
        if field is None:
            raise TemplateExtractionError(
                f"The {field_name} grid does not contain ten bubbles per digit."
            )
        field["required"] = field_name == "roll"
        fields[field_name] = field
        grids[field_name] = {
            "bounds": omr.grid_bounds(np.concatenate(circles, axis=0)),
            "digits": len(field["digits"]),
        }

    return {
        "canvas": [omr.CANVAS_W, omr.CANVAS_H],
        "grids": grids,
        "answers": answers,
        "numbering": numbering,
        "options_per_question": options_per_question,
        "option_labels_by_row": option_labels_by_row,
        "fields": fields,
    }


def _read_groups(
    aligned: np.ndarray,
    groups: list[list[list[float]]],
    diameter: float,
) -> tuple[list[list[float]], float]:
    return omr.read_bubble_groups(aligned, groups, diameter)


def _read_digits(aligned: np.ndarray, field: dict[str, Any]) -> tuple[str | None, bool]:
    values, fit = _read_groups(
        aligned,
        field["digits"],
        float(field["diameter"]),
    )
    if fit < omr.MIN_FIT:
        raise TemplateMismatch(f"Digit-grid fit was below threshold ({fit:.0%}).")
    selected, unclear = omr.decode_marks(values)
    if not field.get("required", True) and not any(selected) and not any(unclear):
        return "", False
    result = []
    for choices, bad in zip(selected, unclear):
        if bad or len(choices) != 1:
            return None, True
        result.append(field["digit_order"][choices[0]])
    return "".join(result), False


def read_with_template(
    sheet,
    structure: dict[str, Any],
    question_numbers: list[int],
    multi_allowed: dict[int, bool],
):
    aligned = omr.align_page(load_gray(sheet.image))
    if aligned is None:
        raise TemplateMismatch("Four corner markers were not found.")
    answers_template = structure["answers"]
    aligned = omr.orient(aligned, answers_template)
    values, fit = omr.read_marks(aligned, answers_template)
    if fit < omr.MIN_FIT:
        raise TemplateMismatch(f"Answer-grid fit was below threshold ({fit:.0%}).")
    marks = omr.answers_by_number(values, answers_template, structure["numbering"])
    order = omr.question_order(answers_template, structure["numbering"])
    try:
        answers = {
            number: [
                structure["option_labels_by_row"][order[number - 1]][ord(label) - 65]
                for label in marks[number][0]
            ]
            for number in question_numbers
        }
        unclear = {
            number
            for number in question_numbers
            if marks[number][1]
            or (len(marks[number][0]) > 1 and not multi_allowed.get(number, False))
        }
    except (KeyError, IndexError) as exc:
        raise TemplateMismatch(
            "The template does not contain every requested question."
        ) from exc
    identity = {}
    unclear_fields = set()
    for field_name, field in structure["fields"].items():
        value, bad = _read_digits(aligned, field)
        identity[field_name] = value or ""
        if bad:
            unclear_fields.add(field_name)
    return {
        "answers": answers,
        "unclear": sorted(unclear),
        "unclear_fields": unclear_fields,
        "invalid": False,
        "name": "",
        "roll": identity.get("roll", ""),
        "registration": identity.get("registration", ""),
        "class_name": "",
        "section": "",
        "subject_code": "",
        "set_code": "",
    }
