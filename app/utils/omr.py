import math
from typing import Any

import cv2
import numpy as np

CANVAS_W, CANVAS_H = 1240, 1754  # A4 at ~150 dpi; every page is warped to this
MARGIN = 40
MIN_FIT = 0.9  # share of bubbles whose outline must be found on a sheet


def _normalize(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape
    small = cv2.resize(
        gray, (max(1, w // 4), max(1, h // 4)), interpolation=cv2.INTER_AREA
    )
    paper = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    bg = cv2.resize(cv2.GaussianBlur(paper, (0, 0), 6), (w, h))
    return np.clip(cv2.divide(gray, bg, scale=255), 0, 255).astype(np.uint8)


def _binary(norm: np.ndarray) -> np.ndarray:
    return cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]


def _find_markers(gray: np.ndarray) -> np.ndarray | None:
    h0, w0 = gray.shape
    scale = min(1.0, 1600 / max(h0, w0))
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    bw = _binary(_normalize(small))
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = small.shape
    corners = [
        (0, 0, w / 3, h / 3),
        (2 * w / 3, 0, w, h / 3),
        (2 * w / 3, 2 * h / 3, w, h),
        (0, 2 * h / 3, w / 3, h),
    ]
    points = []
    for x0, y0, x1, y1 in corners:
        best = None
        for contour in contours:
            (cx, cy), (rw, rh), _ = cv2.minAreaRect(contour)
            area = rw * rh
            if rh == 0 or area < 100 or area > (0.08 * w) ** 2:
                continue
            if not 0.75 <= rw / rh <= 1.33 or cv2.contourArea(contour) / area < 0.8:
                continue
            if x0 <= cx < x1 and y0 <= cy < y1 and (best is None or area > best[2]):
                best = (cx / scale, cy / scale, area)
        if best is None:
            return None
        points.append(best[:2])
    return np.float32(points)


def align_page(gray: np.ndarray) -> np.ndarray | None:
    points = _find_markers(gray)
    if points is None:
        return None
    target = np.float32(
        [
            [MARGIN, MARGIN],
            [CANVAS_W - MARGIN, MARGIN],
            [CANVAS_W - MARGIN, CANVAS_H - MARGIN],
            [MARGIN, CANVAS_H - MARGIN],
        ]
    )
    matrix = cv2.getPerspectiveTransform(points, target)
    return cv2.warpPerspective(gray, matrix, (CANVAS_W, CANVAS_H), borderValue=255)


def _groups(values: np.ndarray, gap: float) -> list[list[int]]:
    order = np.argsort(values)
    groups = [[int(order[0])]]
    for a, b in zip(order[:-1], order[1:]):
        if values[b] - values[a] > gap:
            groups.append([])
        groups[-1].append(int(b))
    return groups


def _circles(bw: np.ndarray) -> np.ndarray:
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (20 <= w <= 70 and 20 <= h <= 70 and 0.8 <= w / h <= 1.25):
            continue
        area, perimeter = cv2.contourArea(contour), cv2.arcLength(contour, True)
        if (
            perimeter
            and 4 * math.pi * area / perimeter**2 >= 0.85
            and area / (w * h) <= 0.88
        ):
            found.append((x + w / 2, y + h / 2, (w + h) / 2))
    return np.asarray(found, dtype=np.float32).reshape((-1, 3))


def bubble_components(aligned: np.ndarray) -> list[np.ndarray]:
    circles = _circles(_binary(_normalize(aligned)))
    if not len(circles):
        return []
    diameter = float(np.median(circles[:, 2]))
    parent = list(range(len(circles)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first, second):
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    for first in range(len(circles)):
        delta = np.abs(circles[first + 1 :, :2] - circles[first, :2])
        neighbors = np.flatnonzero(np.max(delta, axis=1) <= 3.25 * diameter)
        for offset in neighbors:
            union(first, first + 1 + int(offset))

    components: dict[int, list[np.ndarray]] = {}
    for index, circle in enumerate(circles):
        components.setdefault(find(index), []).append(circle)
    return [
        np.asarray(component, dtype=np.float32)
        for component in sorted(
            (items for items in components.values() if len(items) >= 10),
            key=lambda items: (
                float(np.min(np.asarray(items)[:, 1])),
                float(np.min(np.asarray(items)[:, 0])),
            ),
        )
    ]


def annotate_components(
    aligned: np.ndarray, components: list[np.ndarray]
) -> np.ndarray:
    annotated = cv2.cvtColor(aligned, cv2.COLOR_GRAY2BGR)
    for index, component in enumerate(components):
        x0 = int(np.min(component[:, 0] - component[:, 2]))
        y0 = int(np.min(component[:, 1] - component[:, 2]))
        x1 = int(np.max(component[:, 0] + component[:, 2]))
        y1 = int(np.max(component[:, 1] + component[:, 2]))
        cv2.rectangle(annotated, (x0, y0), (x1, y1), (0, 0, 255), 3)
        cv2.putText(
            annotated,
            f"GRID {index + 1}",
            (x0, max(24, y0 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
    return annotated


def template_for_component(
    aligned: np.ndarray, circles: np.ndarray
) -> dict[str, Any] | None:
    return template_from_circles(aligned, circles)


def template_for_components(
    aligned: np.ndarray, components: list[np.ndarray]
) -> dict[str, Any] | None:
    if not components:
        return None
    circles = np.concatenate(components, axis=0)
    template = template_from_circles(aligned, circles)
    if template is None:
        return None
    return template


def digit_columns(circles: np.ndarray) -> list[list[list[float]]] | None:
    diameter = float(np.median(circles[:, 2]))
    if circles[:, 2].max() - circles[:, 2].min() > 0.25 * diameter:
        return None
    columns = _groups(circles[:, 0], 2.7 * diameter)
    result = []
    for column in columns:
        bubbles = circles[column]
        rows = _groups(bubbles[:, 1], 0.8 * diameter)
        if len(rows) != 10 or any(len(row) != 1 for row in rows):
            return None
        ordered = sorted(rows, key=lambda row: float(bubbles[row[0], 1]))
        result.append(
            [[round(float(value), 1) for value in bubbles[row[0]]] for row in ordered]
        )
    return result or None


def _ink(norm: np.ndarray, y0: int, y1: int) -> float:
    band = norm[max(0, y0) : max(1, y1)]
    return float((band < 128).mean()) if band.size else 0.0


def build_template(aligned: np.ndarray) -> dict[str, Any] | None:
    circles = _circles(_binary(_normalize(aligned)))
    return template_from_circles(aligned, circles)


def template_from_circles(
    aligned: np.ndarray, circles: np.ndarray
) -> dict[str, Any] | None:
    if len(circles) < 8:
        return None
    norm = _normalize(aligned)
    diameter = float(np.median(circles[:, 2]))
    if circles[:, 2].max() - circles[:, 2].min() > 0.25 * diameter:
        return None
    columns = _groups(circles[:, 0], 2.7 * diameter)
    rows = []
    for column_index, column in enumerate(columns):
        sub = circles[column]
        for row_index, row in enumerate(
            sorted(
                _groups(sub[:, 1], 0.8 * diameter),
                key=lambda r: float(np.mean(sub[r, 1])),
            )
        ):
            bubbles = sub[row][np.argsort(sub[row][:, 0])]
            if not 2 <= len(bubbles) <= 10:
                return None
            rows.append(
                {
                    "col": column_index,
                    "row": row_index,
                    "bubbles": [[round(float(v), 1) for v in b] for b in bubbles],
                }
            )
    top = min(b[1] for r in rows for b in r["bubbles"]) - 1.6 * diameter
    bottom = max(b[1] for r in rows for b in r["bubbles"]) + 1.6 * diameter
    return {
        "canvas": [CANVAS_W, CANVAS_H],
        "diameter": round(diameter, 1),
        "grid_top": int(top),
        "grid_bottom": int(bottom),
        "ink_top": round(_ink(norm, 0, int(top)), 4),
        "ink_bottom": round(_ink(norm, int(bottom), CANVAS_H), 4),
        "questions": rows,
    }


def orient(aligned: np.ndarray, template: dict[str, Any]) -> np.ndarray:
    norm = _normalize(aligned)
    top = _ink(norm, 0, template["grid_top"])
    bottom = _ink(norm, template["grid_bottom"], CANVAS_H)
    same = abs(top - template["ink_top"]) + abs(bottom - template["ink_bottom"])
    flipped = abs(bottom - template["ink_top"]) + abs(top - template["ink_bottom"])
    return cv2.rotate(aligned, cv2.ROTATE_180) if flipped < same else aligned


def prepare_template_sheet(aligned: np.ndarray) -> np.ndarray:
    norm = _normalize(aligned)
    return (
        cv2.rotate(aligned, cv2.ROTATE_180)
        if _ink(norm, 0, 500) < _ink(norm, CANVAS_H - 500, CANVAS_H) * 0.5
        else aligned
    )


def read_marks(
    aligned: np.ndarray, template: dict[str, Any]
) -> tuple[list[list[float]], float]:
    groups = [question["bubbles"] for question in template["questions"]]
    return read_bubble_groups(aligned, groups, template["diameter"])


def read_bubble_groups(
    aligned: np.ndarray,
    groups: list[list[list[float]]],
    diameter: float,
) -> tuple[list[list[float]], float]:
    norm = _normalize(aligned)
    d = diameter
    half = int(math.ceil(d * 0.95))
    yy, xx = np.mgrid[-half : half + 1, -half : half + 1]
    radius = np.sqrt(xx**2 + yy**2)
    inner, ring = radius <= 0.35 * d, (radius >= 0.44 * d) & (radius <= 0.58 * d)
    around = (radius >= 0.75 * d) & (radius <= 0.95 * d)
    values, fitting, total = [], 0, 0
    for group in groups:
        row = []
        for cx, cy, _ in group:
            x0, y0 = int(round(cx)) - half, int(round(cy)) - half
            roi = norm[y0 : y0 + 2 * half + 1, x0 : x0 + 2 * half + 1]
            total += 1
            if x0 < 0 or y0 < 0 or roi.shape != radius.shape:
                row.append(0.0)
                continue
            dark = 1 - roi.astype(np.float32) / 255
            row.append(float(dark[inner].mean()))
            fitting += (dark[ring].mean() - dark[around].mean()) > 0.06
        values.append(row)
    return values, fitting / max(1, total)


def decode_marks(values: list[list[float]], *, mark=0.2, faint=0.1, relative=0.6):
    selected, unclear = [], []
    for row in values:
        top = max(row)
        chosen = [i for i, v in enumerate(row) if v >= mark and v >= relative * top]
        selected.append(chosen)
        unclear.append(any(v >= faint and i not in chosen for i, v in enumerate(row)))
    return selected, unclear


def header_crop(aligned: np.ndarray, template: dict[str, Any]) -> np.ndarray:
    return aligned[: template["grid_top"]]


def question_order(template: dict[str, Any], numbering: str = "column") -> list[int]:
    rows = template["questions"]
    if numbering == "column":
        return sorted(range(len(rows)), key=lambda i: (rows[i]["col"], rows[i]["row"]))
    return sorted(range(len(rows)), key=lambda i: (rows[i]["row"], rows[i]["col"]))


def answers_by_number(
    values, template, numbering="column"
) -> dict[int, tuple[list[str], bool]]:
    order = question_order(template, numbering)
    selected, unclear = decode_marks([values[i] for i in order])
    return {
        n + 1: ([chr(65 + i) for i in sel], flag or len(sel) > 1)
        for n, (sel, flag) in enumerate(zip(selected, unclear))
    }


def pick_numbering(
    aligned, template, ai_answers, ai_unclear, option_counts, *, minimum=0.9
):
    if len(template["questions"]) != len(option_counts):
        return None
    values, fit = read_marks(aligned, template)
    if fit < MIN_FIT:
        return None
    best, best_score = None, 0.0
    for numbering in ("column", "row"):
        order = question_order(template, numbering)
        if any(
            len(template["questions"][row]["bubbles"]) != option_counts.get(n + 1)
            for n, row in enumerate(order)
        ):
            continue
        read = answers_by_number(values, template, numbering)
        usable = [n for n in read if n not in ai_unclear]
        if not usable:
            continue
        score = sum(read[n][0] == ai_answers.get(n, []) for n in usable) / len(usable)
        if score > best_score:
            best, best_score = numbering, score
    return best if best_score >= minimum else None
