"""End-to-end API tests: upload -> job -> results -> review -> rescore."""

import csv
import io
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from filesystem.models.models import FileModel
from omr.models import Assessment, ProcessJob, Sheet
from omr.tests.sheetgen import encode, render_sheet

MEDIA = tempfile.mkdtemp(prefix="omr-test-media-")
KEY = {str(q): "ABCD"[(q - 1) % 4] for q in range(1, 21)}


def answers_for(key, wrong=(), blank=(), double=()):
    answers = {int(q): v for q, v in key.items()}
    for q in wrong:
        answers[q] = "D" if key[str(q)] != "D" else "A"
    for q in blank:
        answers[q] = None
    for q in double:
        answers[q] = "AB"
    return answers


@override_settings(MEDIA_ROOT=MEDIA, OMR_RUN_JOBS_IN_THREAD=False)
class OmrApiTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        User = get_user_model()
        self.teacher = User.objects.create_user(email="t@example.com", password="x")
        self.other = User.objects.create_user(email="o@example.com", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.teacher)

    def upload(self, data: bytes, name="sheet.jpg", owner=None):
        file_model = FileModel(created_by=owner or self.teacher)
        file_model.file.save(name, ContentFile(data), save=True)
        return file_model.pk

    def create(self, file_ids, key=KEY, **extra):
        return self.client.post(
            "/api/omr/assessments/",
            {"title": "Quiz", "answer_key": key, "file_ids": file_ids, **extra},
            format="json",
        )

    # --- happy path ------------------------------------------------------

    def test_full_flow(self):
        good = self.upload(
            encode(render_sheet(answers_for(KEY, wrong=(2, 5), blank=(9,))))
        )
        flagged = self.upload(
            encode(
                render_sheet(answers_for(KEY, double=(4,)), roll="5512", noise_seed=7)
            )
        )
        junk = self.upload(b"definitely not an image", name="junk.jpg")
        response = self.create([good, flagged, junk])
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["sheets_queued"], 3)

        job = self.client.get(f"/api/omr/jobs/{response.data['job_id']}/").data
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["stage_index"], 9)
        self.assertTrue(all(s["status"] == "done" for s in job["stages"]))
        self.assertEqual(job["summary"]["processed"], 2)
        self.assertEqual(job["summary"]["rejected"], 1)

        assessment_id = response.data["assessment_id"]
        detail = self.client.get(f"/api/omr/assessments/{assessment_id}/").data
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["counts"]["processed"], 2)
        self.assertEqual(detail["counts"]["rejected"], 1)
        self.assertEqual(detail["counts"]["needs_review"], 1)
        by_file = {s["file_id"]: s for s in detail["sheets"]}
        self.assertEqual(by_file[good]["roll_no"], "2031")
        self.assertEqual(by_file[good]["result"]["correct"], 17)
        self.assertEqual(by_file[good]["result"]["wrong"], 2)
        self.assertEqual(by_file[good]["result"]["blank"], 1)
        self.assertEqual(by_file[good]["result"]["percentage"], 85.0)
        self.assertTrue(by_file[good]["result"]["verified"])
        self.assertEqual(by_file[flagged]["roll_no"], "5512")
        self.assertEqual(by_file[junk]["status"], "rejected")
        self.assertTrue(by_file[junk]["reject_reason"])

        # Teacher resolves the double mark on Q4 -> rescored, fully verified.
        sheet_id = by_file[flagged]["id"]
        sheet = self.client.get(f"/api/omr/sheets/{sheet_id}/").data
        q4 = next(q for q in sheet["result"]["per_question"] if q["q"] == 4)
        self.assertEqual(q4["flags"], ["multi_mark"])
        self.assertTrue(q4["crop"].startswith("data:image/png;base64,"))
        self.assertEqual(sheet["result"]["correct"], 19)
        verified = self.client.patch(
            f"/api/omr/sheets/{sheet_id}/verify/",
            {"question_no": 4, "selected": KEY["4"]},
            format="json",
        ).data
        self.assertEqual(verified["result"]["correct"], 20)
        self.assertEqual(verified["result"]["needs_review_count"], 0)
        self.assertTrue(verified["result"]["verified"])

        analytics = self.client.get(
            f"/api/omr/assessments/{assessment_id}/analytics/"
        ).data
        self.assertEqual(analytics["graded"], 2)
        self.assertEqual(analytics["topper"]["roll_no"], "5512")
        self.assertEqual(analytics["average"], 92.5)
        q2 = next(q for q in analytics["question_difficulty"] if q["q"] == 2)
        self.assertEqual((q2["correct"], q2["wrong"]), (1, 1))

        export = self.client.get(f"/api/omr/assessments/{assessment_id}/export/")
        self.assertEqual(export.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(export.content.decode())))
        self.assertEqual(len(rows), 3)
        row = next(r for r in rows if r["roll_no"] == "5512")
        self.assertEqual(row["Q4"], KEY["4"])
        self.assertEqual(row["percentage"], "100.0")

    def test_answer_key_edit_rescores_without_reprocessing(self):
        file_id = self.upload(encode(render_sheet(answers_for(KEY))))
        assessment_id = self.create([file_id]).data["assessment_id"]
        fixed = {**KEY, "1": "D"}  # teacher had Q1 wrong in the key
        response = self.client.patch(
            f"/api/omr/assessments/{assessment_id}/",
            {"answer_key": fixed, "wrong_marks": -1},
            format="json",
        )
        self.assertEqual(response.data["rescored"], 1)
        result = Sheet.objects.get(assessment_id=assessment_id).result
        self.assertEqual((result.correct, result.wrong, result.total), (19, 1, 18))

    def test_key_longer_than_sheet_counts_missing_questions(self):
        key = {**KEY, "21": "A", "22": "B"}  # sheet only prints 20
        file_id = self.upload(encode(render_sheet(answers_for(KEY))))
        assessment_id = self.create([file_id], key=key).data["assessment_id"]
        result = Sheet.objects.get(assessment_id=assessment_id).result
        self.assertEqual(result.scored_questions, 22)
        self.assertEqual(result.correct, 20)
        self.assertEqual(result.needs_review_count, 2)
        flags = {q["q"]: q["flags"] for q in result.per_question}
        self.assertEqual(flags[21], ["not_detected"])

    def test_add_sheets_dedupes_and_reprocess_retries_rejects(self):
        data = encode(render_sheet(answers_for(KEY)))
        first = self.upload(data)
        assessment_id = self.create([first]).data["assessment_id"]
        again = self.upload(data, name="same.jpg")
        bad = self.upload(b"nope", name="bad.jpg")
        response = self.client.post(
            f"/api/omr/assessments/{assessment_id}/sheets/",
            {"file_ids": [again, bad]},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["duplicates"], 1)
        self.assertEqual(response.data["sheets_queued"], 1)
        response = self.client.post(
            f"/api/omr/assessments/{assessment_id}/reprocess/", {}, format="json"
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["sheets_queued"], 1)  # only the reject
        self.assertEqual(
            Sheet.objects.filter(
                assessment_id=assessment_id, status="processed"
            ).count(),
            1,
        )

    def test_roll_number_correction(self):
        file_id = self.upload(encode(render_sheet(answers_for(KEY))))
        assessment_id = self.create([file_id]).data["assessment_id"]
        sheet = Sheet.objects.get(assessment_id=assessment_id)
        response = self.client.patch(
            f"/api/omr/sheets/{sheet.pk}/", {"roll_no": "9999"}, format="json"
        )
        self.assertEqual(response.data["roll_no"], "9999")

    # --- isolation & validation -------------------------------------------

    def test_cannot_use_or_see_another_teachers_data(self):
        foreign = self.upload(encode(render_sheet(answers_for(KEY))), owner=self.other)
        response = self.create([foreign])
        self.assertEqual(response.status_code, 400)  # file isn't ours
        self.assertEqual(Assessment.objects.count(), 0)

        mine = self.upload(encode(render_sheet(answers_for(KEY))))
        created = self.create([mine]).data
        sheet_id = Sheet.objects.get(assessment_id=created["assessment_id"]).pk
        intruder = APIClient()
        intruder.force_authenticate(self.other)
        self.assertEqual(intruder.get("/api/omr/assessments/").data, [])
        for url in (
            f"/api/omr/assessments/{created['assessment_id']}/",
            f"/api/omr/assessments/{created['assessment_id']}/analytics/",
            f"/api/omr/assessments/{created['assessment_id']}/export/",
            f"/api/omr/jobs/{created['job_id']}/",
            f"/api/omr/sheets/{sheet_id}/",
        ):
            self.assertEqual(intruder.get(url).status_code, 404, url)
        response = intruder.patch(
            f"/api/omr/sheets/{sheet_id}/verify/", {"question_no": 1}, format="json"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            intruder.delete(
                f"/api/omr/assessments/{created['assessment_id']}/"
            ).status_code,
            404,
        )

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/omr/assessments/").status_code, 401)

    def test_invalid_answer_key(self):
        file_id = self.upload(encode(render_sheet(answers_for(KEY))))
        for key in ({}, {"x": "A"}, {"1": "AB"}, {"0": "A"}, {"1": "?"}):
            with self.subTest(key=key):
                self.assertEqual(self.create([file_id], key=key).status_code, 400)

    def test_stale_job_is_reaped_and_blocks_nothing(self):
        file_id = self.upload(encode(render_sheet(answers_for(KEY))))
        assessment_id = self.create([file_id]).data["assessment_id"]
        job = ProcessJob.objects.create(assessment_id=assessment_id, status="running")
        ProcessJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(hours=1)
        )
        data = self.client.get(f"/api/omr/jobs/{job.pk}/").data
        self.assertEqual(data["status"], "error")
        response = self.client.post(
            f"/api/omr/assessments/{assessment_id}/reprocess/",
            {"include_processed": True},
            format="json",
        )
        self.assertEqual(response.status_code, 202)

    def test_running_job_blocks_key_edit_and_delete(self):
        file_id = self.upload(encode(render_sheet(answers_for(KEY))))
        assessment_id = self.create([file_id]).data["assessment_id"]
        ProcessJob.objects.create(assessment_id=assessment_id, status="running")
        url = f"/api/omr/assessments/{assessment_id}/"
        self.assertEqual(
            self.client.patch(url, {"answer_key": KEY}, format="json").status_code, 409
        )
        self.assertEqual(
            self.client.patch(url, {"title": "Renamed"}, format="json").status_code, 200
        )
        self.assertEqual(self.client.delete(url).status_code, 409)


@override_settings(MEDIA_ROOT=MEDIA, OMR_RUN_JOBS_IN_THREAD=True)
class ThreadedJobTests(TransactionTestCase):
    """The production path: job launched on commit, runs on a thread."""

    def test_job_runs_in_background(self):
        import time

        teacher = get_user_model().objects.create_user(email="b@example.com", password="x")
        client = APIClient()
        client.force_authenticate(teacher)
        file_model = FileModel(created_by=teacher)
        file_model.file.save(
            "s.jpg", ContentFile(encode(render_sheet(answers_for(KEY)))), save=True
        )
        response = client.post(
            "/api/omr/assessments/",
            {"answer_key": KEY, "file_ids": [file_model.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        deadline = time.monotonic() + 60
        job = None
        while time.monotonic() < deadline:
            job = client.get(f"/api/omr/jobs/{response.data['job_id']}/").data
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.2)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["summary"]["processed"], 1)
