"""Render synthetic OMR sheets (and phone-photo versions) for tests."""

from __future__ import annotations

import cv2
import numpy as np

LETTERS = "ABCDEFGH"


def render_sheet(
    answers: dict[int, str | None],
    n_questions: int = 20,
    n_options: int = 4,
    roll: str = "2031",
    blocks: int = 2,
    noise_seed: int | None = 0,
) -> np.ndarray:
    """A4 @150dpi sheet: title, roll grid (columns x 0-9), answer blocks.

    ``answers`` maps question -> letters to fill ("AB" = double mark,
    None/missing = blank).
    """
    width, height = 1240, 1754
    img = np.full((height, width), 255, np.uint8)
    cv2.putText(img, "MANASI OMR SHEET", (380, 90), cv2.FONT_HERSHEY_SIMPLEX, 1.3, 0, 3)
    radius = 14
    x0, y0 = 120, 180
    cv2.putText(img, "ROLL", (x0, y0 - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    for col, digit in enumerate(roll):
        for value in range(10):
            center = (x0 + col * 40, y0 + value * 38)
            cv2.circle(img, center, radius, 0, 2)
            if int(digit) == value:
                cv2.circle(img, center, radius - 3, 0, -1)
    per_block = (n_questions + blocks - 1) // blocks
    for q in range(n_questions):
        block, row = divmod(q, per_block)
        bx, by = 150 + block * 520, 640 + row * 48
        cv2.putText(
            img, str(q + 1), (bx - 80, by + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 0, 2
        )
        marked = answers.get(q + 1) or ""
        for option in range(n_options):
            cx = bx + option * 50
            cv2.circle(img, (cx, by), radius, 0, 2)
            letter = LETTERS[option]
            cv2.putText(
                img, letter, (cx - 6, by + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 0, 1
            )
            if letter in marked:
                cv2.circle(img, (cx, by), radius - 2, 0, -1)
    if noise_seed is not None:
        rng = np.random.default_rng(noise_seed)
        img = np.clip(img.astype(int) + rng.normal(0, 6, img.shape), 0, 255).astype(
            np.uint8
        )
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def rotate(img: np.ndarray, degrees: float) -> np.ndarray:
    h, w = img.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(img, matrix, (w, h), borderValue=(255, 255, 255))


def photograph(img: np.ndarray, scale: float = 1.0) -> np.ndarray:
    """The sheet lying on a desk, shot at an angle."""
    h, w = img.shape[:2]
    out_w, out_h = int(1700 * scale), int(2200 * scale)
    background = np.full((out_h, out_w, 3), (70, 90, 110), np.uint8)
    corners = np.float32([[180, 140], [1530, 220], [1600, 2080], [110, 2000]]) * scale
    matrix = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [w, 0], [w, h], [0, h]]), corners
    )
    photo = cv2.warpPerspective(
        img, matrix, (out_w, out_h), dst=background, borderMode=cv2.BORDER_TRANSPARENT
    )
    return cv2.GaussianBlur(photo, (3, 3), 0)


def encode(img: np.ndarray, ext: str = ".jpg") -> bytes:
    ok, buffer = cv2.imencode(ext, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    assert ok
    return buffer.tobytes()
