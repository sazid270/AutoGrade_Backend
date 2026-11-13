"""OMR evaluation pipeline — stages 2..8 of the practical flow.

Pragmatic (no strict fiducial template): validate the image, preprocess it,
deskew it, warp the detected page quad, auto-locate the bubble grid from the
print itself, read fill ratios per bubble, flag ambiguity with a confidence
score, then match against the teacher's answer key.

Every sheet goes through the same functions; the job runner in ``views``
walks each stage across the whole batch so the app can show live progress.
"""

from __future__ import annotations

import base64
import hashlib
import math
import statistics
from typing import Any

import cv2
import numpy as np

# --- decision thresholds -------------------------------------------------
INK_MAX_GRAY = 125  # darker than this (post-CLAHE) counts as pen ink
RATIO_MARKED = 0.50  # filled ratio >= -> Marked (real marks land 0.72+)
RATIO_EMPTY = 0.28  # filled ratio <= -> Empty (letter baseline <= 0.19)
EMPTY_CUT_MARGIN = 0.10  # adaptive empty cut = column baseline + this
FLAG_CONFIDENCE_BELOW = 0.60  # below this -> Needs Review
BLUR_MIN_VARIANCE = 30.0  # Laplacian variance; below -> reject as blurry
MIN_BUBBLES = 20  # fewer circles than this -> not an OMR template
# Calibrated on two real sheets. Metrics use the CONVEX HULL so broken
# thin rings still read as discs; timing dashes fail disc (0.33) and
# aspect, printed boxes fail via the radius-group size rule, glyphs pass
# shape but are much smaller than real bubbles.
BUBBLE_FILL_MIN = 0.58  # hull area / enclosing-circle area (arcs ~0.61+)
BUBBLE_CIRC_MIN = 0.62  # 4*pi*hull_area / hull_perimeter^2
RADIUS_CHAIN_RATIO = 1.12  # radii within 12% chain into one size group
RADIUS_GROUP_MIN = 8  # groups smaller than this are noise
RADIUS_KEEP_FRACTION = 0.75  # keep groups at least 75% of the biggest
PITCH_MATCH_RATIO = 1.18  # rows whose column pitch differs more aren't one block
MAX_BLOCK_COLS = 6  # widest single-question block before it gets cut
CROP_MAX_WIDTH = 420  # flagged-question crop served to the reviewer

STAGES: list[tuple[int, str]] = [
    (1, "Teacher Upload / Scan"),
    (2, "Image Validation & Preprocessing"),
    (3, "Sheet Registration (Alignment)"),
    (4, "Field Detection (ROI Cropping)"),
    (5, "Bubble / OMR Recognition"),
    (6, "Ambiguity Handling & Confidence Check"),
    (7, "Answer Matching & Scoring"),
    (8, "Result Generation + Verification"),
    (9, "Storage, Analytics & Reports"),
]


class SheetRejected(Exception):
    """Stage-level rejection with a message the teacher can act on."""


# --- stage 1 helpers -----------------------------------------------------


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


# --- stage 2: validation & preprocessing ---------------------------------


def decode_image(data: bytes) -> np.ndarray:
    if looks_like_pdf(data):
        raise SheetRejected(
            "PDF is not supported yet — export the sheet as JPG or PNG."
        )
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SheetRejected("Unsupported or corrupt image — use JPG or PNG.")
    h, w = img.shape[:2]
    if min(h, w) < 400:
        raise SheetRejected("Image resolution too low — rescan at higher DPI.")
    return img


def blur_variance(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def check_blur(gray: np.ndarray) -> None:
    if blur_variance(gray) < BLUR_MIN_VARIANCE:
        raise SheetRejected("Image too blurry, please rescan.")


def preprocess(img: np.ndarray) -> np.ndarray:
    """Grayscale -> denoise -> contrast normalization."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    denoised = cv2.medianBlur(gray, 3)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(denoised)


TARGET_WIDTH = 1000  # working width the detection thresholds are calibrated at


def normalize_scale(gray: np.ndarray) -> np.ndarray:
    """Resize the (registered) page to the working width.

    Bubble radii, rule lengths and close kernels are calibrated at
    TARGET_WIDTH: small chat re-uploads (480px) are upscaled, and modern
    phone photos (3000-4000px, bubbles 60px+ — wider than the 31px
    rule-removal kernel) are downscaled. Runs after the blur check, which
    must see the original pixels.
    """
    h, w = gray.shape
    if abs(w - TARGET_WIDTH) <= 0.05 * TARGET_WIDTH:
        return gray
    scale = TARGET_WIDTH / w
    return cv2.resize(
        gray,
        (TARGET_WIDTH, max(1, int(round(h * scale)))),
        interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA,
    )


def deskew(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Small-angle deskew via horizontal projection variance."""
    best_angle = 0.0
    best_score = -1.0
    h, w = gray.shape
    for angle in np.arange(-6.0, 6.01, 0.75):
        rotated = _rotate(gray, float(angle), border=255)
        crop = rotated[h // 10 : h - h // 10, w // 10 : w - w // 10]
        _, binary = cv2.threshold(
            crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        score = float(np.var(binary.sum(axis=1)))
        if score > best_score:
            best_score = score
            best_angle = float(angle)
    # Second pass at 0.25 deg around the coarse winner: a 0.4 deg residual
    # drifts a 10-row roll grid by most of a bubble radius.
    for angle in np.arange(best_angle - 0.5, best_angle + 0.51, 0.25):
        if abs(angle - best_angle) < 1e-6:
            continue
        rotated = _rotate(gray, float(angle), border=255)
        crop = rotated[h // 10 : h - h // 10, w // 10 : w - w // 10]
        _, binary = cv2.threshold(
            crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        score = float(np.var(binary.sum(axis=1)))
        if score > best_score:
            best_score = score
            best_angle = float(angle)
    if abs(best_angle) < 0.2:
        return gray, 0.0
    return _rotate(gray, best_angle, border=255), best_angle


def _rotate(gray: np.ndarray, angle: float, border: int) -> np.ndarray:
    h, w = gray.shape
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(
        gray,
        matrix,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(border,),
    )


# --- stage 3: registration (perspective / page quad) ---------------------


def _order_quad(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype("float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(-1)  # y - x
    return np.array(
        [
            pts[np.argmin(s)],  # top-left (x + y minimal)
            pts[np.argmin(d)],  # top-right (y - x minimal)
            pts[np.argmax(s)],  # bottom-right (x + y maximal)
            pts[np.argmax(d)],  # bottom-left (y - x maximal)
        ],
        dtype="float32",
    )


def register(gray: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    """Warp the photographed page to a flat rectangle when a page quad shows.

    Flat, full-bleed scans (quad == image border) are left untouched.
    """
    h, w = gray.shape
    # The page is the big *bright* region; threshold for paper, not ink, and
    # close away the printed content so the page is one solid blob.
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    info: dict[str, Any] = {"warped": False, "angle": 0.0}
    if not contours:
        return gray, info
    contour = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(contour)
    if area < 0.25 * h * w:
        return gray, info
    hull = cv2.convexHull(contour)
    peri = cv2.arcLength(hull, True)
    approx = None
    for eps in (0.02, 0.03, 0.04, 0.05):
        candidate = cv2.approxPolyDP(hull, eps * peri, True)
        if len(candidate) == 4:
            approx = candidate
            break
    if approx is None:
        return gray, info
    quad = _order_quad(approx)
    if cv2.contourArea(quad) > 0.92 * h * w:
        return gray, info  # already a flat crop — warping would distort it
    top = np.linalg.norm(quad[0] - quad[1])
    bottom = np.linalg.norm(quad[3] - quad[2])
    left = np.linalg.norm(quad[0] - quad[3])
    right = np.linalg.norm(quad[1] - quad[2])
    dst_w = int(max(top, bottom))
    dst_h = int(max(left, right))
    if dst_w < 100 or dst_h < 100:
        return gray, info
    dst = np.array(
        [[0, 0], [dst_w, 0], [dst_w, dst_h], [0, dst_h]], dtype="float32"
    )
    matrix = cv2.getPerspectiveTransform(quad, dst)
    warped = cv2.warpPerspective(gray, matrix, (dst_w, dst_h))
    info["warped"] = True
    return warped, info


# --- stage 4: field detection (bubbles, blocks, roll number) -------------


def _remove_rules(binary: np.ndarray) -> np.ndarray:
    """Drop long straight rules (cell borders, separators).

    Without this the ring-close below would fuse the rightmost bubble of a
    row to its box border and the merged contour fails the shape filter.
    """
    # 31px sits between the biggest bubble (~26px) and any real rule, so
    # opening removes borders without slicing filled bubbles in half.
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, np.ones((1, 31), np.uint8)
    )
    vertical = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, np.ones((31, 1), np.uint8)
    )
    return cv2.subtract(binary, cv2.bitwise_or(horizontal, vertical))


def _ink_masks(gray: np.ndarray) -> dict[str, tuple[np.ndarray, ...]]:
    """Rules-removed threshold masks, keyed by detection source name.

    - ``raw``: plain Otsu ink — best for tightly-packed sheets like WBJEE,
      whose 20px row pitch would fuse under any closing.
    - ``closed``: Otsu ink after a 5x5 close x2 — best for sheets whose thin
      rings threshold into broken arcs (malegoan).
    - ``union``: both of the above (each sheet keeps whatever works).
    - ``adaptive``: local-mean threshold — finds pale printed rings whose
      gray (118-150) sits *above* the global Otsu cut (HSC sheets printed
      on pink shading), where the global masks drop every empty bubble.
    """
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    binary = _remove_rules(binary)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 51, 12
    )
    adaptive = _remove_rules(adaptive)
    return {
        "raw": (binary,),
        "closed": (closed,),
        "union": (binary, closed),
        "adaptive": (adaptive,),
    }


def detect_bubbles(
    gray: np.ndarray, source: str = "union"
) -> list[tuple[float, float, float]]:
    """Circular bubble candidates: printed (empty) rings and filled marks.

    Candidates are gathered from one threshold mask (see ``_ink_masks``);
    the layout stage tries several sources and keeps the layout that scores
    best, because no single threshold works on every sheet stock.
    """
    return _bubbles_from_masks(_ink_masks(gray)[source])


def _bubbles_from_masks(
    masks: tuple[np.ndarray, ...],
) -> list[tuple[float, float, float]]:
    """Shape-filter the contours of one or more threshold masks."""
    candidates: list[tuple[float, float, float]] = []
    image_area = masks[0].size
    for mask in masks:
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 25 or area > 0.01 * image_area:
                continue
            hull = cv2.convexHull(contour)
            hull_area = cv2.contourArea(hull)
            hull_perim = cv2.arcLength(hull, True)
            if hull_area <= 0 or hull_perim <= 0:
                continue
            (cx, cy), radius = cv2.minEnclosingCircle(contour)
            if radius < 3:
                continue
            disc_fill = hull_area / (math.pi * radius * radius)
            circularity = 4 * math.pi * hull_area / (hull_perim * hull_perim)
            x, y, bw, bh = cv2.boundingRect(contour)
            aspect = bw / bh if bh else 0.0
            if disc_fill < BUBBLE_FILL_MIN or circularity < BUBBLE_CIRC_MIN:
                continue
            if not 0.65 <= aspect <= 1.50:
                continue
            candidates.append((cx, cy, radius))
    if not candidates:
        return []
    # Chain radii into size groups; keep groups that are both substantial and
    # close in size to the biggest real bubble group. This drops question
    # number glyphs and stray letters while keeping every bubble grid even
    # when grids use slightly different diameters.
    ordered = sorted(candidates, key=lambda b: b[2])
    groups: list[list[tuple[float, float, float]]] = []
    for bubble in ordered:
        if groups and bubble[2] <= groups[-1][-1][2] * RADIUS_CHAIN_RATIO:
            groups[-1].append(bubble)
        else:
            groups.append([bubble])
    substantial = [g for g in groups if len(g) >= RADIUS_GROUP_MIN] or groups
    biggest = max(
        statistics.median(b[2] for b in group) for group in substantial
    )
    kept = [
        b
        for group in substantial
        if statistics.median(b[2] for b in group)
        >= RADIUS_KEEP_FRACTION * biggest
        for b in group
    ]
    # One bubble can yield several contour pieces (broken ring) — keep the
    # largest detection per location.
    kept.sort(key=lambda b: -b[2])
    deduped: list[tuple[float, float, float]] = []
    for bubble in kept:
        if any(
            abs(bubble[0] - seen[0]) < 0.6 * bubble[2]
            and abs(bubble[1] - seen[1]) < 0.6 * bubble[2]
            for seen in deduped
        ):
            continue
        deduped.append(bubble)
    return deduped


LAYOUT_SOURCES = ("closed", "raw", "union", "adaptive")


def detect_layout(
    gray: np.ndarray, n_options: int | None = None
) -> dict[str, Any]:
    """Cluster bubbles into rows, rows into blocks, then classify blocks.

    Several threshold sources are tried (see ``_ink_masks``) and the layout
    that scores best wins: pale-ring sheets need the adaptive mask, packed
    sheets need plain Otsu, thin-ring sheets need the closed mask, and no
    single one of them is right for every sheet stock. ``n_options`` is the
    answer key's option count — with it, blocks are cut to one question
    wide, question-number columns are dropped, and only exactly-n_options
    blocks count as answers.
    """
    masks = _ink_masks(gray)
    best: dict[str, Any] | None = None
    best_score = 0
    saw_bubbles = False
    rolls: list[dict[str, Any]] = []
    for source in LAYOUT_SOURCES:
        bubbles = _bubbles_from_masks(masks[source])
        if len(bubbles) < MIN_BUBBLES:
            continue
        saw_bubbles = True
        try:
            layout, score = _build_layout(gray, bubbles, n_options, source)
        except SheetRejected:
            continue
        if layout["roll_block"] is not None:
            rolls.append(layout["roll_block"])
        if score > best_score:
            best, best_score = layout, score
    if best is not None:
        # The roll grid is usually packed tighter than the answers, so the
        # mask that reads the answers best can fuse it away; take the best
        # roll grid any source found.
        if rolls:
            best["roll_block"] = min(rolls, key=_roll_rank)
        return best
    if not saw_bubbles:
        raise SheetRejected(
            "Not a recognized OMR sheet — wrong template or bad crop?"
        )
    raise SheetRejected(
        "No answer grid found — this does not look like an OMR sheet."
    )


def _x_clusters(rows: list[dict[str, Any]], d: float) -> list[list[float]]:
    """Group a block's pooled x positions into column clusters.

    Chained gaps up to 0.9d absorb within-column jitter (detection noise,
    slight page skew) while the ~2.3d column spacing always breaks.
    """
    xs = sorted(b[0] for row in rows for b in row["items"])
    if not xs:
        return []
    clusters = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] <= 0.9 * d:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return clusters


def _project_rows(
    rows: list[dict[str, Any]], keep_x: set[float]
) -> list[dict[str, Any]]:
    """Drop items outside the kept x set; drop rows left with nothing."""
    out = []
    for row in rows:
        items = [b for b in row["items"] if b[0] in keep_x]
        if items:
            out.append(
                {
                    "y": statistics.fmean(b[1] for b in items),
                    "items": items,
                    "xs": [b[0] for b in items],
                }
            )
    return out


def _trim_to_lattice(
    rows: list[dict[str, Any]], d: float, n_options: int | None
) -> list[dict[str, Any]]:
    """Drop off-lattice detections and the rows they were the only item of.

    A neighbouring field (registration grid, roll strip) can share a row's
    y-band and sit within the segment split distance, so its rows bridge
    into an answer block and inflate its row count. Columns backed by
    fewer than half of the biggest column's detections are that field's,
    not the answer grid's — unless the block is too sparse to tell
    (fills-only sheets), where leaving it alone is the safe default.
    """
    clusters = _x_clusters(rows, d)
    if not clusters:
        return rows
    sizes = [len(c) for c in clusters]
    keep = [c for c, size in zip(clusters, sizes) if size >= 0.5 * max(sizes)]
    if len(keep) < max(3, n_options or 3):
        return rows
    return _project_rows(rows, {x for c in keep for x in c})


def _cut_block(
    rows: list[dict[str, Any]], d: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a too-wide block at its largest column gap."""
    centers = [statistics.median(c) for c in _x_clusters(rows, d)]
    gaps = [
        (centers[i + 1] - centers[i], i) for i in range(len(centers) - 1)
    ]
    _, index = max(gaps)
    cut = (centers[index] + centers[index + 1]) / 2
    left: list[dict[str, Any]] = []
    right: list[dict[str, Any]] = []
    for row in rows:
        for side, items in (
            ("left", [b for b in row["items"] if b[0] < cut]),
            ("right", [b for b in row["items"] if b[0] >= cut]),
        ):
            if items:
                piece = {
                    "y": statistics.fmean(b[1] for b in items),
                    "items": items,
                    "xs": [b[0] for b in items],
                }
                (left if side == "left" else right).append(piece)
    return left, right


def _refine_block(
    rows: list[dict[str, Any]], d: float, n_options: int | None
) -> list[list[dict[str, Any]]]:
    """Clean one answer-sized block: trim off-lattice items, then keep
    cutting blocks wider than one question (two questions printed side by
    side with no x-gap between them) into per-question pieces."""
    if len(rows) >= 12:
        rows = _trim_to_lattice(rows, d, n_options)
        if not rows:
            return []
    if len(_x_clusters(rows, d)) > MAX_BLOCK_COLS:
        left, right = _cut_block(rows, d)
        if left and right:
            out: list[list[dict[str, Any]]] = []
            for group in (left, right):
                out.extend(_refine_block(group, d, n_options))
            return out
    return [rows]


def _strip_label(
    rows: list[dict[str, Any]], d: float, n_options: int
) -> list[dict[str, Any]]:
    """Drop the leading (or trailing) question-number column when the
    remaining n_options columns form a regular lattice — the HSC-style
    digit column printed just left of option A."""
    clusters = _x_clusters(rows, d)
    if len(clusters) != n_options + 1:
        return rows
    centers = [statistics.median(c) for c in clusters]
    for drop_last in (False, True):
        keep = centers[:-1] if drop_last else centers[1:]
        gaps = [keep[i + 1] - keep[i] for i in range(len(keep) - 1)]
        if not gaps:
            continue
        median_gap = statistics.median(gaps)
        if median_gap <= 0:
            continue
        if max(abs(gap - median_gap) for gap in gaps) > 0.18 * median_gap:
            continue
        drop_x = set(clusters[-1] if drop_last else clusters[0])
        out = _project_rows(rows, _all_x(rows) - drop_x)
        return out if out else []
    return rows


def _all_x(rows: list[dict[str, Any]]) -> set[float]:
    return {b[0] for row in rows for b in row["items"]}


def _block_infos(
    blocks: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    infos = []
    for rows_ in blocks:
        if not rows_:
            continue
        ys = [row["y"] for row in rows_]
        # Mode, not first row: a row with a missed bubble must not change
        # how the block is classified.
        lengths = [len(r["xs"]) for r in rows_]
        infos.append(
            {
                "rows": rows_,
                "cols": max(set(lengths), key=lengths.count),
                "count": len(rows_),
                "top": min(ys),
                "bottom": max(ys),
            }
        )
    return infos


def _build_layout(
    gray: np.ndarray,
    bubbles: list[tuple[float, float, float]],
    n_options: int | None,
    source: str,
) -> tuple[dict[str, Any], int]:
    """Build (layout, score) from one detection source's bubble set."""
    median_r = statistics.median(r for _, _, r in bubbles)
    d = 2 * median_r  # nominal bubble diameter
    rows = _group_rows(bubbles, d)
    # A visual row often spans several side-by-side column blocks; split it
    # into contiguous x-segments first so each block groups independently.
    units: list[dict[str, Any]] = []
    for row in rows:
        for segment in _split_segments(row["items"], d):
            units.append(
                {
                    "y": row["y"],
                    "items": segment,
                    "xs": [b[0] for b in segment],
                }
            )
    units.sort(key=lambda u: u["y"])
    refined: list[list[dict[str, Any]]] = []
    for block in _group_blocks(units, d):
        refined.extend(_refine_block(block, d, n_options))
    infos = _block_infos(refined)
    page_h = gray.shape[0]
    for info in infos:
        info["digit_grid"] = _looks_like_digit_grid(gray, info, d)

    def _rank(info: dict[str, Any]) -> tuple[int, int]:
        # Ties (e.g. two 10x4 answer blocks) go to the block whose rows
        # carry one mark each — the answer-like fill pattern.
        return (info["cols"] * info["count"], info.get("single_rows", 0))

    candidates = [
        i
        for i in infos
        if 3 <= i["cols"] <= 6 and i["count"] >= 8 and not i["digit_grid"]
    ]
    if not candidates:
        raise SheetRejected(
            "No answer grid found — this does not look like an OMR sheet."
        )
    # The key's option count is the strongest signal for which grid is the
    # answer grid (a 52x12 name grid or a 10x6 roll strip is not).
    if n_options:
        exact = [i for i in candidates if i["cols"] == n_options]
        if exact:
            candidates = exact
    anchor = max(candidates, key=_rank)
    header_cut = anchor["top"] - 0.12 * page_h
    answers = [
        i
        for i in infos
        if abs(i["cols"] - anchor["cols"]) <= 1
        and i["count"] >= max(6, anchor["count"] // 2)
        and i["top"] >= header_cut
        and not i["digit_grid"]
    ]
    answers.sort(key=lambda i: (min(x for x in i["rows"][0]["xs"]), i["top"]))

    # Roll number: digit grids (6..11 rows). They may sit above *or beside*
    # the answer blocks depending on the template.
    digit_blocks = [
        i
        for i in infos
        if 6 <= i["count"] <= 11
        and 1 <= i["cols"] <= 11
        and i not in answers
    ]
    roll_block = None
    if digit_blocks:
        # Prefer a true 0-9 grid (10 rows), else the tallest, else topmost —
        # e.g. WBJEE's roll grid (7 rows) beats its exam-code grid (6 rows),
        # and the HSC sheet's roll grid sits *beside* the answers, above the
        # registration grid and subject-code strip (all 10 rows).
        digit_blocks.sort(key=_roll_rank)
        roll_block = digit_blocks[0]

    # With the key known, answer blocks must land on exactly n_options
    # columns: strip a question-number column, then drop anything else.
    if n_options:
        stripped = []
        for info in answers:
            # _strip_label returns one block's rows, not a list of blocks.
            rows = (
                _strip_label(info["rows"], d, n_options)
                if info["cols"] == n_options + 1
                else info["rows"]
            )
            stripped.extend(_block_infos([rows]))
        answers = [i for i in stripped if i["cols"] == n_options]
        answers.sort(
            key=lambda i: (min(x for x in i["rows"][0]["xs"]), i["top"])
        )
    if not answers:
        raise SheetRejected(
            "No answer grid found — this does not look like an OMR sheet."
        )

    questions = []
    number = 1
    for block in answers:
        block_rows = sorted(block["rows"], key=lambda r: r["y"])
        columns = _block_columns(block_rows, d)
        if not columns:
            continue
        # Grid points, not raw detections: score every (row, column) pair at
        # the column's median x. A filled blob that was missed or detected
        # off-centre (pen tail) then cannot shift the option letters, and an
        # empty ring anchors each column.
        radius = statistics.median(
            b[2] for row in block_rows for b in row["items"]
        )
        for row in block_rows:
            questions.append(
                {
                    "q": number,
                    "bubbles": [(x, row["y"], radius) for x in columns],
                }
            )
            number += 1
    # Sources that only find the marks (or only the junk) score low here;
    # a real grid gives every answer block n_options columns and rows that
    # actually carry their options.
    score = 0
    need = (n_options - 1) if n_options else None
    for block in answers:
        threshold = need if need is not None else max(3, block["cols"] - 1)
        score += sum(
            1 for row in block["rows"] if len(row["xs"]) >= threshold
        )
    layout = {
        "diameter": d,
        "bubbles_found": len(bubbles),
        "blocks": len(answers),
        "questions": questions,
        "roll_block": roll_block,
        "layout_note": (
            f"{len(answers)} answer block(s), {len(questions)} rows, "
            f"{len(bubbles)} bubbles ({source} mask)"
        ),
    }
    return layout, score


def _roll_rank(info: dict[str, Any]) -> tuple:
    """Sort key for roll-number grid candidates (smaller is better)."""
    return (
        0 if info.get("digit_grid") else 1,
        0 if info["count"] == 10 else 1,
        -info["count"],
        -info["cols"],
        info["top"],
    )


def _looks_like_digit_grid(
    gray: np.ndarray, info: dict[str, Any], d: float
) -> bool:
    """True for a filled roll/registration grid: one mark per *column*.

    A 4-digit roll grid is geometrically identical to a 10-question,
    4-option answer block, so geometry alone cannot tell them apart. The
    fill pattern can: answers carry (at most) one mark per row, while a
    digit grid carries exactly one mark per column spread over few rows.
    Also stores ``single_rows`` (rows with exactly one mark) on ``info``.
    """
    rows = sorted(info["rows"], key=lambda r: r["y"])
    columns = _block_columns(rows, d)
    info["single_rows"] = 0
    if len(columns) < 2 or not rows:
        return False
    radius = statistics.median(b[2] for row in rows for b in row["items"])
    marks = [
        [ink_ratio(gray, x, row["y"], radius) >= RATIO_MARKED for x in columns]
        for row in rows
    ]
    per_row = [sum(row) for row in marks]
    per_col = [sum(row[c] for row in marks) for c in range(len(columns))]
    info["single_rows"] = sum(1 for n in per_row if n == 1)
    if not 6 <= len(rows) <= 11:
        return False
    if any(n != 1 for n in per_col):
        return False
    # Every column has exactly one mark. An answer block looks like that
    # only if most of its rows are blank; a digit grid's marks share rows
    # (e.g. roll 2031 marks 4 cells over 4 rows of 10 -> 6 blank rows).
    marked_rows = sum(1 for n in per_row if n)
    multi_rows = sum(1 for n in per_row if n > 1)
    return multi_rows > 0 or marked_rows <= 0.6 * len(rows)


def _group_rows(
    bubbles: list[tuple[float, float, float]], d: float
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bubble in sorted(bubbles, key=lambda b: b[1]):
        target = None
        for row in rows:
            if abs(row["y"] - bubble[1]) <= 0.45 * d:
                target = row
                break
        if target is None:
            rows.append({"y": bubble[1], "items": [bubble]})
        else:
            target["items"].append(bubble)
    for row in rows:
        row["items"].sort(key=lambda b: b[0])
        row["xs"] = [b[0] for b in row["items"]]
        row["y"] = statistics.fmean(b[1] for b in row["items"])
    rows.sort(key=lambda r: r["y"])
    return rows


def _split_segments(
    items: list[tuple[float, float, float]], d: float
) -> list[list[tuple[float, float, float]]]:
    """Split one visual row into contiguous runs (one run per column block)."""
    segments: list[list[tuple[float, float, float]]] = [[items[0]]]
    for bubble in items[1:]:
        if bubble[0] - segments[-1][-1][0] <= 2.5 * d:
            segments[-1].append(bubble)
        else:
            segments.append([bubble])
    return segments


def _block_columns(rows: list[dict[str, Any]], d: float) -> list[float]:
    """X centers of a block's option columns.

    Splits all detected x positions at the K-1 largest gaps, where K is the
    most common per-row bubble count. Large pen-tail outliers only add small
    gaps, so the true column boundaries still win and strays get absorbed
    into the neighbouring column's median.
    """
    xs = sorted(b[0] for row in rows for b in row["items"])
    if not xs:
        return []
    lengths = [len(row["xs"]) for row in rows]
    k = max(set(lengths), key=lengths.count)
    if k <= 1 or len(xs) < k:
        return [statistics.fmean(xs)] if xs else []
    gaps = sorted(
        ((xs[i + 1] - xs[i], i) for i in range(len(xs) - 1)), reverse=True
    )
    cuts = sorted(index for _, index in gaps[: k - 1])
    clusters: list[list[float]] = []
    start = 0
    for cut in cuts:
        clusters.append(xs[start : cut + 1])
        start = cut + 1
    clusters.append(xs[start:])
    return [statistics.fmean(c) for c in clusters if c]


def _group_blocks(rows: list[dict[str, Any]], d: float) -> list[list[dict[str, Any]]]:
    """Rows sharing column positions and stacked together = one block.

    Matching is against the union of x positions seen in the block (not just
    the first row), length differences are tolerated when the overlap is
    strong, and two pieces of the same visual row (split because a filled
    bubble went undetected) are merged back instead of counted twice.
    """
    blocks: list[list[dict[str, Any]]] = []
    known: list[list[float]] = []
    bottoms: list[float] = []
    pitches: list[list[float]] = []
    for row in rows:
        placed = False
        pitch = _row_pitch(row["xs"], d)
        for index, block in enumerate(blocks):
            # A different column pitch means a different printed field (a
            # 32px-pitch roll grid sitting right above a 40px-pitch answer
            # block shares enough x positions to look like one block).
            if pitch and pitches[index]:
                block_pitch = statistics.median(pitches[index])
                ratio = max(pitch, block_pitch) / min(pitch, block_pitch)
                if ratio > PITCH_MATCH_RATIO:
                    continue
            overlap = sum(
                1
                for x in row["xs"]
                if any(abs(x - kx) <= 0.6 * d for kx in known[index])
            )
            if overlap == 0:
                continue
            # Same visual row as an existing member -> it was split earlier.
            for existing in block:
                if abs(existing["y"] - row["y"]) <= 0.45 * d:
                    existing["items"].extend(row["items"])
                    existing["items"].sort(key=lambda b: b[0])
                    existing["xs"] = [b[0] for b in existing["items"]]
                    known[index].extend(row["xs"])
                    placed = True
                    break
            if placed:
                break
            if abs(row["y"] - bottoms[index]) > 3.5 * d:
                continue
            need = 1 if len(row["xs"]) == 1 else max(2, int(0.6 * len(row["xs"])))
            if overlap >= need:
                block.append(row)
                bottoms[index] = row["y"]
                known[index].extend(row["xs"])
                if pitch:
                    pitches[index].append(pitch)
                placed = True
                break
        if not placed:
            blocks.append([row])
            known.append(list(row["xs"]))
            bottoms.append(row["y"])
            pitches.append([pitch] if pitch else [])
    return blocks


def _row_pitch(xs: list[float], d: float) -> float | None:
    """Column pitch of one row: its smallest neighbour gap (a missed bubble
    only adds a double gap, it never shrinks the smallest one)."""
    gaps = [b - a for a, b in zip(xs, xs[1:]) if b - a > 0.5 * d]
    return min(gaps) if len(gaps) >= 2 else None


def read_roll_no(gray: np.ndarray, roll_block: dict[str, Any] | None) -> tuple[str, str]:
    """Read the roll-number digit columns; returns (value, confidence).

    Reads on a fitted lattice (column centers x row pitch), not on the list
    of detected bubbles: a single missed ring would otherwise shift every
    digit below it in that column.
    """
    if roll_block is None:
        return "", "none"
    rows = sorted(roll_block["rows"], key=lambda r: r["y"])
    if roll_block["cols"] < 3 and len(_split_columns(rows)) < 3:
        return "", "none"
    radius = statistics.median(b[2] for row in rows for b in row["items"])
    d = 2 * radius
    columns = [statistics.median(c) for c in _x_clusters(rows, d)]
    if len(columns) < 3:
        return "", "none"
    ys = [row["y"] for row in rows]
    steps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.5 * d]
    if not steps:
        return "", "none"
    pitch = statistics.median(steps)
    slots = int(round((ys[-1] - ys[0]) / pitch)) + 1
    if not 6 <= slots <= 11:
        return "", "none"
    lattice: list[float] = []
    for k in range(slots):
        guess = ys[0] + k * pitch
        near = [y for y in ys if abs(y - guess) <= 0.4 * pitch]
        lattice.append(min(near, key=lambda y: abs(y - guess)) if near else guess)
    digits: list[str] = []
    clean = True
    for x in columns:
        marked = [
            index
            for index, y in enumerate(lattice)
            if ink_ratio(gray, x, y, radius) >= RATIO_MARKED
        ]
        if len(marked) == 1:
            digits.append(str(marked[0]))
        else:
            digits.append("?")
            clean = False
    value = "".join(digits)
    if not value or value.count("?") > len(digits) // 2:
        return "", "none"
    # 10 slots of 0..9 stacked top-to-bottom means the index IS the digit.
    if clean and slots == 10 and len(digits) >= 3:
        return value, "high"
    return value, "low"


def _split_columns(
    rows: list[dict[str, Any]],
) -> list[list[tuple[float, float, float]]]:
    all_bubbles = sorted(
        (b for row in rows for b in row["items"]), key=lambda b: b[0]
    )
    if not all_bubbles:
        return []
    xs = [b[0] for b in all_bubbles]
    gaps = [(xs[i + 1] - xs[i], i) for i in range(len(xs) - 1)]
    if not gaps:
        return [all_bubbles]
    biggest_gap = max(gaps)[0]
    median_r = statistics.median(b[2] for b in all_bubbles)
    cut = max(1.6 * median_r, 0.5 * biggest_gap)
    columns: list[list[tuple[float, float, float]]] = [[]]
    previous_x = all_bubbles[0][0]
    columns[0].append(all_bubbles[0])
    for bubble in all_bubbles[1:]:
        if bubble[0] - previous_x > cut:
            columns.append([])
        columns[-1].append(bubble)
        previous_x = bubble[0]
    return [c for c in columns if c]


# --- stage 5: bubble recognition -----------------------------------------


def ink_ratio(gray: np.ndarray, cx: float, cy: float, r: float) -> float:
    inner = max(2.0, 0.78 * r)
    x0, x1 = int(round(cx - inner)), int(round(cx + inner))
    y0, y1 = int(round(cy - inner)), int(round(cy + inner))
    patch = gray[max(0, y0) : y1 + 1, max(0, x0) : x1 + 1]
    if patch.size == 0:
        return 0.0
    return float(np.count_nonzero(patch < INK_MAX_GRAY)) / patch.size


def _ink_ratio(gray: np.ndarray, cx: float, cy: float, r: float) -> float:
    return ink_ratio(gray, cx, cy, r)


def recognize(gray: np.ndarray, layout: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill ratio per option bubble -> raw per-question records."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ratios = [
        [round(ink_ratio(gray, cx, cy, r), 3) for cx, cy, r in item["bubbles"]]
        for item in layout["questions"]
    ]
    empty_cuts = _empty_cuts(ratios)
    records = []
    for item, row_ratios in zip(layout["questions"], ratios):
        bubbles = item["bubbles"]
        options = []
        for index, (cx, cy, r) in enumerate(bubbles):
            ratio = row_ratios[index]
            if ratio >= RATIO_MARKED:
                state = "marked"
            elif ratio <= empty_cuts.get(index, RATIO_EMPTY):
                state = "empty"
            else:
                state = "ambiguous"
            options.append(
                {
                    "letter": letters[index] if index < len(letters) else "?",
                    "ratio": ratio,
                    "state": state,
                    "box": [
                        int(cx - r),
                        int(cy - r),
                        int(cx + r),
                        int(cy + r),
                    ],
                }
            )
        xs = [b[0] - b[2] for b in bubbles] + [b[0] + b[2] for b in bubbles]
        ys = [b[1] - b[2] for b in bubbles] + [b[1] + b[2] for b in bubbles]
        marked = [o["letter"] for o in options if o["state"] == "marked"]
        records.append(
            {
                "q": item["q"],
                "box": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))],
                "options": options,
                "selected": marked[0] if len(marked) == 1 else None,
                "multi": len(marked) > 1,
            }
        )
    return records


def _empty_cuts(ratios: list[list[float]]) -> dict[int, float]:
    """Per-option-column "empty" cutoff, adapted to this sheet's print.

    A printed letter inside the ring (or denser paper shading) lifts the
    empty baseline differently per column, so a fixed cut flags clean
    blanks as ambiguous. Most bubbles in any column are empty, so the
    column median is that column's empty baseline.
    """
    cuts: dict[int, float] = {}
    width = max((len(row) for row in ratios), default=0)
    for index in range(width):
        column = [row[index] for row in ratios if index < len(row)]
        if len(column) < 4:
            continue
        baseline = statistics.median(column)
        if baseline >= RATIO_EMPTY + 0.12:
            continue  # mostly marked column (tiny key) — keep the default
        cuts[index] = min(
            RATIO_MARKED - EMPTY_CUT_MARGIN,
            max(RATIO_EMPTY, baseline + EMPTY_CUT_MARGIN),
        )
    return cuts


# --- stage 6: ambiguity & confidence -------------------------------------


def flag_questions(
    gray: np.ndarray,
    records: list[dict[str, Any]],
    key: dict[str, str],
    expected_options: int,
) -> int:
    """Attach confidence/flags and crops for anything needing a human.

    Keyed questions the layout stage never found are appended as
    ``not_detected`` records, so they are reviewed and scored (as blank
    until verified) instead of silently shrinking the denominator.
    """
    keyed = {int(q) for q in key}
    needs_review = 0
    for record in records:
        options = record["options"]
        ratios = [o["ratio"] for o in options]
        flags: list[str] = []
        if record["multi"]:
            flags.append("multi_mark")
            confidence = 0.25
        elif any(o["state"] == "ambiguous" for o in options):
            flags.append("ambiguous")
            confidence = 0.4
        elif record["selected"]:
            confidence = min(
                1.0,
                max(0.0, (max(ratios) - RATIO_EMPTY) / (0.9 - RATIO_EMPTY)),
            )
            if confidence < FLAG_CONFIDENCE_BELOW:
                flags.append("low_confidence")
        else:
            confidence = 0.95  # confidently blank
        if len(options) < expected_options:
            flags.append("missing_bubbles")
        record["confidence"] = round(confidence, 2)
        # raw_flags survive so a later key edit can re-flag the question;
        # questions outside the teacher's key are never queued for review.
        record["raw_flags"] = flags
        record["flags"] = flags if record["q"] in keyed else []
        _reset_review_fields(record)
        if record["flagged"]:
            needs_review += 1
        record["crop"] = (
            _crop_uri(gray, record["box"], _bubble_gap(gray)) if flags else None
        )
    needs_review += add_missing_questions(records, key)
    return needs_review


def _reset_review_fields(record: dict[str, Any]) -> None:
    record["flagged"] = bool(record["flags"])
    record["expected"] = None
    record["outcome"] = None
    record["marks"] = 0
    record["verified"] = False
    record["verified_selected"] = None


def add_missing_questions(
    records: list[dict[str, Any]], key: dict[str, str]
) -> int:
    """Append a flagged placeholder for every keyed question with no record.

    Returns how many were added. Also used when a teacher extends the key
    after processing, so new questions show up for review.
    """
    present = {record["q"] for record in records}
    added = 0
    for question in sorted(int(q) for q in key):
        if question in present:
            continue
        record = {
            "q": question,
            "box": None,
            "options": [],
            "selected": None,
            "multi": False,
            "confidence": 0.0,
            "raw_flags": ["not_detected"],
            "flags": ["not_detected"],
            "crop": None,
        }
        _reset_review_fields(record)
        records.append(record)
        added += 1
    records.sort(key=lambda r: r["q"])
    return added


def _bubble_gap(gray: np.ndarray) -> float:
    """Padding reference for crops; ~1.5% of the page width."""
    return max(12.0, 0.015 * gray.shape[1])


def _crop_uri(gray: np.ndarray, box: list[int], d: float) -> str | None:
    x0, y0, x1, y1 = box
    pad_x, pad_y = 2.4 * d, 1.1 * d
    h, w = gray.shape
    crop = gray[
        max(0, int(y0 - pad_y)) : min(h, int(y1 + pad_y)),
        max(0, int(x0 - pad_x)) : min(w, int(x1 + pad_x)),
    ]
    if crop.size == 0:
        return None
    crop_h, crop_w = crop.shape
    if crop_w > CROP_MAX_WIDTH:
        scale = CROP_MAX_WIDTH / crop_w
        crop = cv2.resize(
            crop, (CROP_MAX_WIDTH, max(1, int(crop_h * scale))), interpolation=cv2.INTER_AREA
        )
    ok, buf = cv2.imencode(".png", crop)
    if not ok:
        return None
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


# --- stage 7: answer matching & scoring ----------------------------------

GRADE_BANDS = (
    (90, "A+"),
    (80, "A"),
    (70, "B"),
    (60, "C"),
    (50, "D"),
)


def grade_for(percentage: float) -> str:
    for threshold, grade in GRADE_BANDS:
        if percentage >= threshold:
            return grade
    return "F"


def score_records(
    records: list[dict[str, Any]],
    key: dict[str, str],
    correct_marks: int,
    wrong_marks: int,
    blank_marks: int,
) -> dict[str, Any]:
    """Stage 7 — also reused by the teacher's verify endpoint."""
    totals = {
        "total": 0,
        "correct": 0,
        "wrong": 0,
        "blank": 0,
        "scored_questions": 0,
    }
    for record in records:
        expected = key.get(str(record["q"]))
        if expected is None:
            record["expected"] = None
            record["outcome"] = "unkeyed"
            record["marks"] = 0
            continue
        selected = (
            record.get("verified_selected")
            if record.get("verified_selected") is not None or record.get("verified")
            else record.get("selected")
        )
        record["expected"] = expected
        totals["scored_questions"] += 1
        if selected is None:
            record["outcome"] = "blank"
            record["marks"] = blank_marks
            totals["blank"] += 1
        elif selected == expected:
            record["outcome"] = "correct"
            record["marks"] = correct_marks
            totals["correct"] += 1
        else:
            record["outcome"] = "wrong"
            record["marks"] = wrong_marks
            totals["wrong"] += 1
        totals["total"] += record["marks"]
    possible = totals["scored_questions"] * correct_marks
    percentage = round(100.0 * totals["total"] / possible, 1) if possible else 0.0
    totals["percentage"] = percentage
    totals["grade"] = grade_for(percentage)
    return totals


# --- stage 8: result payload ---------------------------------------------


def expected_option_count(key: dict[str, str]) -> int:
    """Options per question implied by the key: at least 4 (A-D), more when
    the key uses a later letter (an "E" answer means 5 options)."""
    highest = max(
        (ord(v.upper()) - ord("A") + 1 for v in key.values() if v),
        default=0,
    )
    return max(4, highest)
