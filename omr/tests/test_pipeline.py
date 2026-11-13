"""Accuracy of the image pipeline on synthetic sheets (no database)."""

import random

from django.test import SimpleTestCase

from omr import pipeline
from omr.tests.sheetgen import encode, photograph, render_sheet, rotate


def process(data: bytes, key: dict[str, str]):
    gray = pipeline.preprocess(pipeline.decode_image(data))
    pipeline.check_blur(gray)
    gray, registration = pipeline.register(gray)
    gray, _ = pipeline.deskew(gray)
    gray = pipeline.normalize_scale(gray)
    options = pipeline.expected_option_count(key)
    layout = pipeline.detect_layout(gray, options)
    roll = pipeline.read_roll_no(gray, layout["roll_block"])
    records = pipeline.recognize(gray, layout)
    needs = pipeline.flag_questions(gray, records, key, options)
    totals = pipeline.score_records(records, key, 1, 0, 0)
    return {
        "records": {r["q"]: r for r in records},
        "roll": roll,
        "needs": needs,
        "totals": totals,
        "warped": registration["warped"],
    }


def make_case(n_questions=20, seed=1):
    rng = random.Random(seed)
    key = {str(q): rng.choice("ABCD") for q in range(1, n_questions + 1)}
    answers = {
        q: (key[str(q)] if q % 5 else rng.choice("ABCD"))
        for q in range(1, n_questions + 1)
    }
    answers[3] = None  # blank
    answers[7] = "AB"  # double mark
    return key, answers


class PipelineAccuracyTests(SimpleTestCase):
    def assert_reads(self, result, answers, n_questions):
        for q in range(1, n_questions + 1):
            marked = answers.get(q) or ""
            expected = marked if len(marked) == 1 else None
            record = result["records"][q]
            self.assertEqual(record["selected"], expected, f"Q{q}")
            self.assertEqual(record["multi"], len(marked) > 1, f"Q{q} multi")
        # Only the double mark needs a human.
        self.assertEqual(result["needs"], 1)
        self.assertEqual(result["records"][7]["flags"], ["multi_mark"])
        self.assertEqual(result["roll"], ("2031", "high"))

    def test_flat_scan(self):
        key, answers = make_case()
        result = process(encode(render_sheet(answers)), key)
        self.assert_reads(result, answers, 20)
        self.assertFalse(result["warped"])

    def test_roll_grid_is_not_mistaken_for_answers(self):
        # A 4-digit roll grid is a 10x4 grid exactly like a 10-question block.
        key, answers = make_case()
        result = process(encode(render_sheet(answers)), key)
        self.assertEqual(result["totals"]["scored_questions"], 20)
        self.assertEqual(len([q for q in result["records"] if q <= 20]), 20)

    def test_tilted_scans(self):
        key, answers = make_case(seed=2)
        for degrees in (-4, 2.5):
            with self.subTest(degrees=degrees):
                img = rotate(render_sheet(answers, noise_seed=3), degrees)
                self.assert_reads(process(encode(img), key), answers, 20)

    def test_phone_photo_with_perspective(self):
        key, answers = make_case(seed=3)
        sheet = render_sheet(answers, noise_seed=None)
        for scale in (1.0, 2.0):  # 1700px and 3400px wide photos
            with self.subTest(scale=scale):
                result = process(encode(photograph(sheet, scale)), key)
                self.assertTrue(result["warped"])
                self.assert_reads(result, answers, 20)

    def test_forty_questions(self):
        key, answers = make_case(n_questions=40, seed=4)
        result = process(encode(render_sheet(answers, n_questions=40)), key)
        self.assert_reads(result, answers, 40)

    def test_scoring_against_key(self):
        key, answers = make_case(seed=5)
        totals = process(encode(render_sheet(answers)), key)["totals"]
        correct = sum(1 for q in range(1, 21) if answers.get(q) == key[str(q)])
        self.assertEqual(totals["correct"], correct)
        self.assertEqual(totals["blank"], 2)  # Q3 blank + Q7 double mark
        self.assertEqual(totals["wrong"], 20 - correct - 2)
        self.assertEqual(totals["scored_questions"], 20)


class ValidationTests(SimpleTestCase):
    def test_rejects_non_images_and_pdfs(self):
        with self.assertRaises(pipeline.SheetRejected):
            pipeline.decode_image(b"not an image")
        with self.assertRaises(pipeline.SheetRejected):
            pipeline.decode_image(b"%PDF-1.7 ...")

    def test_rejects_blank_page(self):
        import numpy as np

        blank = np.full((1754, 1240, 3), 255, np.uint8)
        blank[100:110, 100:1100] = 0  # one rule so it isn't "blurry"
        gray = pipeline.normalize_scale(pipeline.preprocess(blank))
        with self.assertRaises(pipeline.SheetRejected):
            pipeline.detect_layout(gray, 4)


class ScoringTests(SimpleTestCase):
    def test_missing_keyed_questions_are_flagged_and_scored(self):
        records = []
        key = {"1": "A", "2": "B"}
        added = pipeline.add_missing_questions(records, key)
        self.assertEqual(added, 2)
        self.assertTrue(all(r["flags"] == ["not_detected"] for r in records))
        totals = pipeline.score_records(records, key, 1, 0, 0)
        # Denominator is the whole key, not just what was detected.
        self.assertEqual(totals["scored_questions"], 2)
        self.assertEqual(totals["percentage"], 0.0)

    def test_negative_marking_and_verification(self):
        records = [
            {"q": 1, "selected": "A"},
            {"q": 2, "selected": "C"},
            {"q": 3, "selected": None},
            {"q": 4, "selected": None, "verified": True, "verified_selected": "D"},
        ]
        key = {"1": "A", "2": "B", "3": "C", "4": "D"}
        totals = pipeline.score_records(records, key, 4, -1, 0)
        self.assertEqual(totals["total"], 4 - 1 + 0 + 4)
        self.assertEqual(totals["percentage"], round(100 * 7 / 16, 1))

    def test_option_count_follows_highest_letter(self):
        self.assertEqual(pipeline.expected_option_count({"1": "A"}), 4)
        self.assertEqual(pipeline.expected_option_count({"1": "A", "2": "E"}), 5)
