from pathlib import Path
from typing import Any


def _letter(index: int) -> str:
    if not 0 <= index < 26:
        raise ValueError("Bubble option index must be between 0 and 25.")
    return chr(ord("A") + index)


def _require_cv2() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "OMR processing requires opencv-python-headless and numpy."
        ) from exc
    return cv2, np


def _fill_ratio(binary: Any, roi: list[int], cv2: Any) -> float:
    x, y, width, height = (int(value) for value in roi)
    if min(width, height) <= 0 or x < 0 or y < 0:
        raise ValueError(
            "Bubble ROI must have positive dimensions and non-negative origin."
        )
    cropped = binary[y : y + height, x : x + width]
    if cropped.shape != (height, width):
        raise ValueError("Bubble ROI lies outside the warped image.")
    return float(cv2.countNonZero(cropped)) / float(width * height)


def read_bubbles(
    binary: Any,
    bubbles: list[list[int]],
    *,
    threshold: float = 0.5,
    confidence_gap: float = 0.08,
    allow_multiple: bool = False,
    cv2: Any,
) -> dict[str, Any]:
    if not 0 < threshold < 1:
        raise ValueError("threshold must be between 0 and 1.")
    ratios = [_fill_ratio(binary, bubble, cv2) for bubble in bubbles]
    marked = [index for index, ratio in enumerate(ratios) if ratio >= threshold]
    ambiguous_floor = min(threshold - confidence_gap, threshold * 0.7)
    ambiguous = [
        index
        for index, ratio in enumerate(ratios)
        if ambiguous_floor <= ratio < threshold
    ]
    if not marked:
        flag = "low_confidence" if ambiguous else "blank"
    elif len(marked) > 1 and not allow_multiple:
        flag = "multiple"
    else:
        flag = "ok"
    top_ratio = max(ratios, default=0.0)
    confidence = (
        max(0.0, top_ratio - threshold) / max(threshold, 1 - threshold)
        if marked
        else 0.0
    )
    if marked and abs(top_ratio - threshold) < confidence_gap:
        flag = "low_confidence"
    return {
        "selected": marked,
        "selected_options": [_letter(index) for index in marked],
        "flag": flag,
        "confidence": round(min(confidence, 1.0), 4),
        "fill_ratios": [round(ratio, 4) for ratio in ratios],
    }


def _read_student_field(
    binary: Any,
    config: dict[str, Any],
    *,
    threshold: float,
    confidence_gap: float,
    cv2: Any,
) -> dict[str, Any]:
    values = []
    confidences = []
    flags = []
    for column in config.get("columns", []):
        result = read_bubbles(
            binary,
            column["bubbles"],
            threshold=threshold,
            confidence_gap=confidence_gap,
            cv2=cv2,
        )
        selected = result["selected"]
        symbols = column.get("symbols", "0123456789")
        if len(selected) != 1 or selected[0] >= len(symbols):
            values.append("")
        else:
            values.append(symbols[selected[0]])
        confidences.append(result["confidence"])
        flags.append(result["flag"])
    valid = bool(values) and all(flag == "ok" for flag in flags) and all(values)
    return {
        "value": "".join(values) if valid else "",
        "confidence": min(confidences) if confidences else None,
        "flag": "ok" if valid else "needs_review",
    }


def _align_image(image: Any, template: dict[str, Any], cv2: Any, np: Any) -> Any:
    width, height = template.get("warp_size", (2100, 2970))
    if template.get("alignment") == "none":
        return image
    working_width = 1600
    scale = working_width / max(image.shape[1], 1)
    working = cv2.resize(
        image,
        (working_width, max(1, round(image.shape[0] * scale))),
        interpolation=cv2.INTER_AREA,
    )
    marker_fraction = float(template.get("marker_size_fraction", 0.02))
    marker_size = max(8, round(working_width * marker_fraction))
    max_marker_size = round(
        working_width * float(template.get("max_marker_size_fraction", 0.08))
    )
    marker_fill_threshold = float(template.get("marker_fill_threshold", 0.65))
    blurred_background = cv2.GaussianBlur(working, (0, 0), 21)
    normalized = cv2.divide(working, blurred_background, scale=255)
    gray = cv2.GaussianBlur(normalized, (5, 5), 0)
    _, thresholded = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    contours, _ = cv2.findContours(
        thresholded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    regions = [
        (0, 0, working.shape[1] // 3, working.shape[0] // 3),
        (working.shape[1] * 2 // 3, 0, working.shape[1], working.shape[0] // 3),
        (
            working.shape[1] * 2 // 3,
            working.shape[0] * 2 // 3,
            working.shape[1],
            working.shape[0],
        ),
        (0, working.shape[0] * 2 // 3, working.shape[1] // 3, working.shape[0]),
    ]
    candidates = []
    for region in regions:
        rx, ry, rw, rh = region
        region_candidates = []
        for contour in contours:
            x, y, contour_width, contour_height = cv2.boundingRect(contour)
            area = contour_width * contour_height
            center_x = x + contour_width / 2
            center_y = y + contour_height / 2
            if (
                rx <= center_x < rw
                and ry <= center_y < rh
                and area >= marker_size**2
                and area <= max_marker_size**2
                and 0.8 <= contour_width / contour_height <= 1.2
                and cv2.contourArea(contour) / area >= 0.9
            ):
                marker = thresholded[y : y + contour_height, x : x + contour_width]
                if cv2.countNonZero(marker) / area < marker_fill_threshold:
                    continue
                region_candidates.append((center_x / scale, center_y / scale, area))
        if region_candidates:
            candidates.append(max(region_candidates, key=lambda point: point[2]))
    if len(candidates) != 4:
        raise ValueError("Could not detect one corner marker in each page corner.")
    asymmetric_corner = template.get("asymmetric_marker_corner")
    if asymmetric_corner is not None:
        if not isinstance(asymmetric_corner, int) or not 0 <= asymmetric_corner < 4:
            raise ValueError("asymmetric_marker_corner must be between 0 and 3.")
        expected_area = candidates[asymmetric_corner][2]
        other_areas = [
            candidate[2]
            for index, candidate in enumerate(candidates)
            if index != asymmetric_corner
        ]
        factor = float(template.get("asymmetric_marker_factor", 1.25))
        if expected_area**0.5 < max(other_areas) ** 0.5 * factor:
            raise ValueError("The sheet orientation does not match the template.")
    source = np.float32([[point[0], point[1]] for point in candidates])
    center = source.mean(axis=0)
    ordered = sorted(source, key=lambda point: (point[1] >= center[1], point[0]))
    top = sorted(ordered[:2], key=lambda point: point[0])
    bottom = sorted(ordered[2:], key=lambda point: point[0], reverse=True)
    source = np.float32(top + bottom)
    marker_points = template.get("corner_markers")
    destination = (
        np.float32(marker_points)
        if marker_points
        else np.float32(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
        )
    )
    matrix = cv2.getPerspectiveTransform(source, destination)
    return cv2.warpPerspective(image, matrix, (width, height))


def process_image(
    image_path: str | Path,
    template: dict[str, Any],
) -> dict[str, Any]:
    """Read an aligned OMR sheet using a validated template configuration."""
    cv2, np = _require_cv2()
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
    width, height = template.get("warp_size", (2100, 2970))
    if width <= 0 or height <= 0:
        raise ValueError("warp_size must contain positive dimensions.")
    image = _align_image(image, template, cv2, np)
    if image.shape[::-1] != (width, height):
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    blurred_background = cv2.GaussianBlur(image, (0, 0), 21)
    normalized = cv2.divide(image, blurred_background, scale=255)
    _, binary = cv2.threshold(
        normalized, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    threshold = float(template.get("fill_threshold", 0.5))
    confidence_gap = float(template.get("confidence_gap", 0.08))

    answers = {}
    for question in template.get("answers", []):
        number = str(question["number"])
        answers[number] = read_bubbles(
            binary,
            question["bubbles"],
            threshold=threshold,
            confidence_gap=confidence_gap,
            allow_multiple=bool(question.get("allow_multiple", False)),
            cv2=cv2,
        )

    roll_columns = template.get("roll_number", {}).get("columns", [])
    roll_values = []
    roll_confidences = []
    roll_flags = []
    for column in roll_columns:
        result = read_bubbles(
            binary,
            column["bubbles"],
            threshold=threshold,
            confidence_gap=confidence_gap,
            cv2=cv2,
        )
        roll_values.append(
            result["selected"][0] if len(result["selected"]) == 1 else None
        )
        roll_confidences.append(result["confidence"])
        roll_flags.append(result["flag"])
    roll_no = (
        "".join(str(value) for value in roll_values)
        if roll_values and all(value is not None for value in roll_values)
        else None
    )
    student_fields = {
        name: _read_student_field(
            binary,
            config,
            threshold=threshold,
            confidence_gap=confidence_gap,
            cv2=cv2,
        )
        for name, config in template.get("student_fields", {}).items()
    }
    if "roll_no" in student_fields:
        roll_no = student_fields["roll_no"]["value"] or None
        roll_confidence = student_fields["roll_no"]["confidence"]
        roll_flag = student_fields["roll_no"]["flag"]
    else:
        roll_confidence = min(roll_confidences) if roll_confidences else None
        roll_flag = (
            "ok"
            if roll_no is not None and all(flag == "ok" for flag in roll_flags)
            else "needs_review"
        )
    return {
        "roll_no": roll_no,
        "roll_confidence": roll_confidence,
        "roll_flag": roll_flag,
        "answers": answers,
        "student_fields": student_fields,
    }
