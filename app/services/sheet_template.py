from typing import Any

import numpy as np
from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.ai_provider import (
    AIProviderError,
    array_content,
    output_text,
    parse_json_object,
    request_ai_provider,
)
from app.utils import omr
from app.utils.options import BENGALI_DIGITS, normalize_option_label


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
    candidates = []
    for index, circles in enumerate(components, start=1):
        diameter = float(np.median(circles[:, 2]))
        xs = omr._groups(circles[:, 0], 2.7 * diameter)
        ys = omr._groups(circles[:, 1], 0.8 * diameter)
        candidates.append(
            {
                "grid_id": index,
                "bubbles": len(circles),
                "x_groups": len(xs),
                "y_groups": len(ys),
                "bounds": [
                    round(float(circles[:, 0].min()), 1),
                    round(float(circles[:, 1].min()), 1),
                    round(float(circles[:, 0].max()), 1),
                    round(float(circles[:, 1].max()), 1),
                ],
            }
        )
    prompt = (
        "Classify the numbered bubble groups on this answer-sheet image. The red "
        "rectangles and GRID labels identify computer-detected groups; one logical "
        "grid may be split into several groups, so label every fragment with the same "
        "role. Do not estimate coordinates. Return only JSON with a grids array, "
        "question_count, options_per_question, option_labels, roll_digit_count, and "
        "registration_digit_count (null when absent). Counts refer to the full logical "
        "grid, not each fragment. For each group include "
        '"grid_id", "role" (answers, roll, registration, or ignore), "numbering" '
        '(column or row for answers), and "digit_order" (the ten digits from top to '
        "bottom for roll/registration). For the answer grid, return option_labels as "
        "one array per question in printed question order, with each question's labels "
        "from left to right. Only identify a group as roll/registration if "
        "it is a 10-choice digit bubble grid. A digit_order must contain each of "
        '0 through 9 exactly once. Example: {"grids":[{"grid_id":1,"role":"answers",'
        '"numbering":"column"},{"grid_id":2,"role":"roll","digit_order":'
        '["0","1","2","3","4","5","6","7","8","9"]}],"question_count":2,'
        '"options_per_question":[4,4],"option_labels":[["A","B","C","D"],'
        '["A","B","C","D"]],"roll_digit_count":6,"registration_digit_count":null}'
    )
    image = omr.annotate_components(aligned, components)
    response = request_ai_provider(
        [
            {"type": "text", "text": prompt + f"\nDetected groups: {candidates}"},
            array_content(image, quality=90),
        ],
        model=settings.OPENROUTER_ANSWER_MODEL,
    )
    try:
        payload = parse_json_object(output_text(response))
    except AIProviderError as exc:
        raise TemplateExtractionError(
            "The AI could not return a valid grid classification."
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
        role = label.get("role")
        if isinstance(role, str) and role in roles:
            roles[role].append((grid_id, label))
    if not roles["answers"] or not roles["roll"]:
        raise TemplateExtractionError(
            "An answer grid and a roll digit grid are required."
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
    answers = omr.template_for_components(
        aligned,
        [components[grid_id - 1] for grid_id, _ in roles["answers"]],
    )
    if answers is None:
        raise TemplateExtractionError(
            "The answer grid could not be verified geometrically."
        )
    order = omr.question_order(answers, numbering)
    options_per_question = [len(answers["questions"][row]["bubbles"]) for row in order]
    if not options_per_question or any(
        not 2 <= count <= 10 for count in options_per_question
    ):
        raise TemplateExtractionError("The detected answer grid is incomplete.")
    question_count = classification.get("question_count")
    if (
        type(question_count) is not int
        or question_count != len(options_per_question)
        or classification.get("options_per_question") != options_per_question
    ):
        raise TemplateExtractionError(
            "The AI's answer-grid counts do not match the detected bubbles."
        )
    raw_option_labels = classification.get("option_labels")
    if not isinstance(raw_option_labels, list) or len(raw_option_labels) != len(order):
        raise TemplateExtractionError(
            "The AI did not identify every answer option label."
        )
    option_labels_by_row = [[] for _ in answers["questions"]]
    for row_index, labels, expected_count in zip(
        order,
        raw_option_labels,
        options_per_question,
    ):
        if not isinstance(labels, list) or len(labels) != expected_count:
            raise TemplateExtractionError(
                "The AI's option labels do not match the detected bubbles."
            )
        normalized = [normalize_option_label(label) for label in labels]
        if any(label is None for label in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise TemplateExtractionError(
                "The AI returned invalid or duplicate option labels."
            )
        option_labels_by_row[row_index] = normalized

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
        field["required"] = True
        expected_count = classification.get(f"{field_name}_digit_count")
        if type(expected_count) is not int or expected_count != len(field["digits"]):
            raise TemplateExtractionError(
                f"The AI's {field_name} digit count does not match the detected grid."
            )
        fields[field_name] = field
    if (
        not roles["registration"]
        and classification.get("registration_digit_count") is not None
    ):
        raise TemplateExtractionError(
            "The AI identified a registration grid that was not detected."
        )

    return {
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
    result = []
    for choices, bad in zip(selected, unclear):
        if bad or len(choices) != 1:
            return None, True
        result.append(field["digit_order"][choices[0]])
    return "".join(result), False


def read_with_template(sheet, structure: dict[str, Any], question_numbers: list[int]):
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
        if marks[number][1] or len(marks[number][0]) > 1
    }
    identity = {}
    unclear_fields = set()
    for field_name, field in structure["fields"].items():
        value, bad = _read_digits(aligned, field)
        identity[field_name] = value or ""
        if bad and field.get("required", True):
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
