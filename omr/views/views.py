"""OMR API endpoints.

Every object is scoped to its creator: another teacher's assessment, sheet
or job is a 404, never a 403, so ids can't be probed. The pipeline itself
runs in ``omr.jobs``.
"""

from __future__ import annotations

import csv
import logging

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt import authentication as jwt_authentication

from filesystem.models.models import FileModel
from omr import jobs, pipeline
from omr.models import Assessment, ProcessJob, Sheet, SheetResult
from omr.serializers import (
    AddSheetsSerializer,
    AssessmentCreateSerializer,
    AssessmentUpdateSerializer,
    SheetRollSerializer,
    SheetVerifySerializer,
)

logger = logging.getLogger(__name__)

RESCORE_FIELDS = {"answer_key", "correct_marks", "wrong_marks", "blank_marks"}
HISTOGRAM_BUCKETS = (
    ("0-40", float("-inf"), 40),
    ("40-60", 40, 60),
    ("60-70", 60, 70),
    ("70-80", 70, 80),
    ("80-90", 80, 90),
    ("90-100", 90, float("inf")),
)


def first_error(errors) -> str:
    """Flatten DRF's error dict to the first human-readable message."""
    value = next(iter(errors.values())) if isinstance(errors, dict) else errors
    while isinstance(value, (list, dict)):
        value = next(iter(value.values() if isinstance(value, dict) else value))
    return str(value)


def bad_request(message: str) -> Response:
    return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)


def conflict(message: str) -> Response:
    return Response({"error": message}, status=status.HTTP_409_CONFLICT)


class OmrView(APIView):
    authentication_classes = [
        jwt_authentication.JWTAuthentication,
        SessionAuthentication,
    ]
    permission_classes = [IsAuthenticated]

    def owned_assessment(self, pk: int) -> Assessment:
        return get_object_or_404(Assessment, pk=pk, created_by=self.request.user)

    def owned_sheet(self, pk: int) -> Sheet:
        return get_object_or_404(
            Sheet.objects.select_related("assessment", "file"),
            pk=pk,
            assessment__created_by=self.request.user,
        )


# --- payload builders -----------------------------------------------------


def result_summary(result: SheetResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "total": result.total,
        "correct": result.correct,
        "wrong": result.wrong,
        "blank": result.blank,
        "scored_questions": result.scored_questions,
        "percentage": result.percentage,
        "grade": result.grade,
        "needs_review_count": result.needs_review_count,
        "verified": result.verified,
    }


def _result_of(sheet: Sheet) -> SheetResult | None:
    try:
        return sheet.result
    except SheetResult.DoesNotExist:
        return None


def sheet_row(sheet: Sheet) -> dict:
    return {
        "id": sheet.pk,
        "file_id": sheet.file_id,
        "file_name": sheet.file_name,
        "roll_no": sheet.roll_no,
        "roll_confidence": sheet.roll_confidence,
        "status": sheet.status,
        "reject_reason": sheet.reject_reason,
        "result": result_summary(_result_of(sheet)),
    }


def sheet_detail(sheet: Sheet, request) -> dict:
    result = _result_of(sheet)
    payload = sheet_row(sheet)
    payload["width"] = sheet.width
    payload["height"] = sheet.height
    payload["created_at"] = sheet.created_at.isoformat()
    payload["image_url"] = (
        request.build_absolute_uri(sheet.file.file.url)
        if sheet.file is not None and sheet.file.file
        else None
    )
    if result is not None:
        payload["result"] = {
            **result_summary(result),
            "detected_questions": result.detected_questions,
            "layout": result.layout,
            "per_question": result.per_question,
        }
    return payload


def job_payload(job: ProcessJob | None) -> dict | None:
    if job is None:
        return None
    return {
        "id": job.pk,
        "assessment_id": job.assessment_id,
        "status": job.status,
        "stage_index": job.stage_index,
        "stage_name": job.stage_name,
        "stages": job.stages,
        "sheets_total": job.sheets_total,
        "sheets_done": job.sheets_done,
        "error": job.error,
        "summary": job.summary,
        "created_at": job.created_at.isoformat(),
    }


def with_counts(queryset):
    return queryset.annotate(
        n_sheets=Count("sheets", distinct=True),
        n_processed=Count(
            "sheets", filter=Q(sheets__status="processed"), distinct=True
        ),
        n_rejected=Count("sheets", filter=Q(sheets__status="rejected"), distinct=True),
        n_pending=Count("sheets", filter=Q(sheets__status="pending"), distinct=True),
        n_review=Sum(
            "sheets__result__needs_review_count",
            filter=Q(sheets__status="processed"),
        ),
    )


def assessment_row(assessment: Assessment) -> dict:
    return {
        "id": assessment.pk,
        "title": assessment.title,
        "subject": assessment.subject,
        "class_name": assessment.class_name,
        "exam_date": assessment.exam_date,
        "assessment_type": assessment.assessment_type,
        "status": assessment.status,
        "question_count": len(assessment.answer_key),
        "created_at": assessment.created_at.isoformat(),
        "counts": {
            "sheets": assessment.n_sheets,
            "processed": assessment.n_processed,
            "rejected": assessment.n_rejected,
            "pending": assessment.n_pending,
            "needs_review": assessment.n_review or 0,
        },
    }


def attach_files(assessment: Assessment, file_ids: list[int], user) -> dict:
    """Create pending Sheets for the caller's uploads; skip re-uploads.

    Only the caller's own FileModel rows are accepted. Duplicates are by
    content hash, so the same photo uploaded twice is graded once.
    """
    files = {
        f.pk: f for f in FileModel.objects.filter(pk__in=file_ids, created_by=user)
    }
    missing = [pk for pk in file_ids if pk not in files]
    known = set(assessment.sheets.values_list("content_hash", flat=True))
    queued = duplicates = 0
    unreadable: list[int] = []
    for pk in file_ids:
        file_model = files.get(pk)
        if file_model is None:
            continue
        try:
            with file_model.file.open("rb") as handle:
                digest = pipeline.content_hash(handle.read())
        except (FileNotFoundError, OSError, ValueError):
            unreadable.append(pk)
            continue
        if digest in known:
            duplicates += 1
            continue
        known.add(digest)
        Sheet.objects.create(
            created_by=user,
            assessment=assessment,
            file=file_model,
            content_hash=digest,
            file_name=file_model.file.name.rsplit("/", 1)[-1],
        )
        queued += 1
    return {
        "sheets_queued": queued,
        "duplicates": duplicates,
        "missing_file_ids": missing + unreadable,
    }


def rescore_assessment(assessment: Assessment) -> int:
    results = SheetResult.objects.select_for_update().filter(
        sheet__assessment=assessment, sheet__status="processed"
    )
    count = 0
    for result in results:
        jobs.rescore_result(result, assessment)
        count += 1
    return count


# --- assessments -------------------------------------------------------------


class AssessmentListCreate(OmrView):
    def get(self, request):
        jobs.reap_stale_jobs()
        queryset = with_counts(
            Assessment.objects.filter(created_by=request.user)
        ).order_by("-created_at")
        return Response([assessment_row(a) for a in queryset])

    def post(self, request):
        serializer = AssessmentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return bad_request(first_error(serializer.errors))
        data = serializer.validated_data
        with transaction.atomic():
            assessment = Assessment.objects.create(
                created_by=request.user,
                title=data["title"],
                subject=data["subject"],
                class_name=data["class_name"],
                exam_date=data["exam_date"],
                assessment_type=data["assessment_type"],
                answer_key=data["answer_key"],
                correct_marks=data["correct_marks"],
                wrong_marks=data["wrong_marks"],
                blank_marks=data["blank_marks"],
                status="processing",
            )
            attached = attach_files(assessment, data["file_ids"], request.user)
            if not attached["sheets_queued"]:
                transaction.set_rollback(True)
                return bad_request(
                    "No readable sheets to process — upload images first."
                )
            job = jobs.start_job(assessment)
        return Response(
            {"assessment_id": assessment.pk, "job_id": job.pk, **attached},
            status=status.HTTP_202_ACCEPTED,
        )


class AssessmentDetail(OmrView):
    def get(self, request, pk):
        jobs.reap_stale_jobs()
        assessment = get_object_or_404(
            with_counts(Assessment.objects.filter(created_by=request.user)), pk=pk
        )
        payload = assessment_row(assessment)
        payload.update(
            answer_key=assessment.answer_key,
            correct_marks=assessment.correct_marks,
            wrong_marks=assessment.wrong_marks,
            blank_marks=assessment.blank_marks,
            latest_job=job_payload(assessment.jobs.order_by("-id").first()),
            sheets=[
                sheet_row(s)
                for s in assessment.sheets.select_related("result").order_by("id")
            ],
        )
        return Response(payload)

    def patch(self, request, pk):
        assessment = self.owned_assessment(pk)
        serializer = AssessmentUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return bad_request(first_error(serializer.errors))
        data = serializer.validated_data
        if (
            RESCORE_FIELDS & data.keys()
            and assessment.jobs.filter(status__in=jobs.ACTIVE_STATUSES).exists()
        ):
            return conflict("Sheets are still processing — try again when done.")
        with transaction.atomic():
            for field, value in data.items():
                setattr(assessment, field, value)
            assessment.save()
            rescored = (
                rescore_assessment(assessment) if RESCORE_FIELDS & data.keys() else 0
            )
        return Response({"id": assessment.pk, "rescored": rescored})

    def delete(self, request, pk):
        assessment = self.owned_assessment(pk)
        jobs.reap_stale_jobs(assessment)
        if assessment.jobs.filter(status__in=jobs.ACTIVE_STATUSES).exists():
            return conflict("Sheets are still processing — try again when done.")
        assessment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AssessmentAddSheets(OmrView):
    """Grade more sheets for an existing assessment (new job)."""

    def post(self, request, pk):
        assessment = self.owned_assessment(pk)
        serializer = AddSheetsSerializer(data=request.data)
        if not serializer.is_valid():
            return bad_request(first_error(serializer.errors))
        try:
            with transaction.atomic():
                attached = attach_files(
                    assessment, serializer.validated_data["file_ids"], request.user
                )
                if not attached["sheets_queued"]:
                    return Response(
                        {"job_id": None, **attached}, status=status.HTTP_200_OK
                    )
                job = jobs.start_job(assessment)
        except jobs.JobAlreadyRunning:
            return conflict("Sheets are still processing — try again when done.")
        return Response(
            {"assessment_id": assessment.pk, "job_id": job.pk, **attached},
            status=status.HTTP_202_ACCEPTED,
        )


class AssessmentReprocess(OmrView):
    """Re-run the pipeline on rejected sheets (or every sheet).

    Body: {"include_processed": false, "sheet_ids": [..optional..]}.
    Reprocessing a processed sheet discards the teacher's verifications on it.
    """

    def post(self, request, pk):
        assessment = self.owned_assessment(pk)
        include_processed = bool(request.data.get("include_processed", False))
        sheet_ids = request.data.get("sheet_ids")
        statuses = ["rejected", "pending"] + (
            ["processed"] if include_processed else []
        )
        sheets = assessment.sheets.filter(status__in=statuses)
        if sheet_ids is not None:
            if not isinstance(sheet_ids, list) or not all(
                isinstance(i, int) for i in sheet_ids
            ):
                return bad_request("sheet_ids must be a list of integers.")
            sheets = sheets.filter(pk__in=sheet_ids)
        try:
            with transaction.atomic():
                if assessment.jobs.filter(status__in=jobs.ACTIVE_STATUSES).exists():
                    jobs.reap_stale_jobs(assessment)
                count = sheets.update(status="pending", reject_reason="")
                if not count:
                    return bad_request("Nothing to reprocess.")
                job = jobs.start_job(assessment)
        except jobs.JobAlreadyRunning:
            return conflict("Sheets are still processing — try again when done.")
        return Response(
            {"assessment_id": assessment.pk, "job_id": job.pk, "sheets_queued": count},
            status=status.HTTP_202_ACCEPTED,
        )


class AssessmentAnalytics(OmrView):
    """Stage 9 — class analytics computed from stored results."""

    def get(self, request, pk):
        assessment = self.owned_assessment(pk)
        results = list(
            SheetResult.objects.filter(
                sheet__assessment=assessment, sheet__status="processed"
            ).select_related("sheet")
        )
        if not results:
            return Response(
                {
                    "sheets": assessment.sheets.count(),
                    "graded": 0,
                    "average": 0.0,
                    "highest": 0.0,
                    "lowest": 0.0,
                    "pass_rate": 0.0,
                    "topper": None,
                    "question_difficulty": [],
                    "grade_distribution": {},
                    "score_histogram": [
                        {"bucket": label, "count": 0}
                        for label, _, _ in HISTOGRAM_BUCKETS
                    ],
                }
            )
        percentages = [r.percentage for r in results]
        by_question: dict[int, dict[str, int]] = {
            int(q): {"correct": 0, "wrong": 0, "blank": 0}
            for q in assessment.answer_key
        }
        option_picks: dict[int, dict[str, int]] = {q: {} for q in by_question}
        for result in results:
            for entry in result.per_question:
                bucket = by_question.get(entry.get("q"))
                if bucket is None:
                    continue
                outcome = entry.get("outcome")
                if outcome in bucket:
                    bucket[outcome] += 1
                picked = (
                    entry.get("verified_selected")
                    if entry.get("verified")
                    else entry.get("selected")
                )
                if picked:
                    picks = option_picks[entry["q"]]
                    picks[picked] = picks.get(picked, 0) + 1
        difficulty = []
        for question in sorted(by_question):
            counts = by_question[question]
            attempted = sum(counts.values())
            difficulty.append(
                {
                    "q": question,
                    "expected": assessment.answer_key.get(str(question)),
                    **counts,
                    "options": option_picks[question],
                    # % of students who did NOT get it right.
                    "difficulty": round(
                        100 * (counts["wrong"] + counts["blank"]) / attempted, 1
                    )
                    if attempted
                    else 0.0,
                }
            )
        grade_distribution: dict[str, int] = {}
        for result in results:
            grade = result.grade or "?"
            grade_distribution[grade] = grade_distribution.get(grade, 0) + 1
        top = max(results, key=lambda r: r.percentage)
        return Response(
            {
                "sheets": assessment.sheets.count(),
                "graded": len(results),
                "average": round(sum(percentages) / len(percentages), 1),
                "highest": max(percentages),
                "lowest": min(percentages),
                "pass_rate": round(
                    100 * sum(1 for r in results if r.grade != "F") / len(results), 1
                ),
                "topper": {
                    "sheet_id": top.sheet_id,
                    "roll_no": top.sheet.roll_no or top.sheet.file_name,
                    "percentage": top.percentage,
                    "grade": top.grade,
                },
                "question_difficulty": difficulty,
                "grade_distribution": grade_distribution,
                "score_histogram": [
                    {
                        "bucket": label,
                        "count": sum(1 for p in percentages if low <= p < high),
                    }
                    for label, low, high in HISTOGRAM_BUCKETS
                ],
            }
        )


class AssessmentExport(OmrView):
    """CSV report: one row per sheet, one column per keyed question."""

    def get(self, request, pk):
        assessment = self.owned_assessment(pk)
        questions = sorted(int(q) for q in assessment.answer_key)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        safe_title = (
            "".join(c if c.isalnum() or c in "-_" else "_" for c in assessment.title)[
                :60
            ]
            or "assessment"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{safe_title}_results.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "sheet_id",
                "file_name",
                "roll_no",
                "roll_confidence",
                "status",
                "total",
                "max_total",
                "percentage",
                "grade",
                "correct",
                "wrong",
                "blank",
                "needs_review",
                "verified",
                *[f"Q{q}" for q in questions],
            ]
        )
        max_total = len(questions) * assessment.correct_marks
        for sheet in assessment.sheets.select_related("result").order_by(
            "roll_no", "id"
        ):
            result = _result_of(sheet)
            answers: dict[int, str] = {}
            if result is not None:
                for entry in result.per_question:
                    picked = (
                        entry.get("verified_selected")
                        if entry.get("verified")
                        else entry.get("selected")
                    )
                    answers[entry.get("q")] = picked or ""
            writer.writerow(
                [
                    sheet.pk,
                    sheet.file_name,
                    sheet.roll_no,
                    sheet.roll_confidence,
                    sheet.status
                    if sheet.status != "rejected"
                    else f"rejected: {sheet.reject_reason}",
                    *(
                        [
                            result.total,
                            max_total,
                            result.percentage,
                            result.grade,
                            result.correct,
                            result.wrong,
                            result.blank,
                            result.needs_review_count,
                            result.verified,
                        ]
                        if result is not None
                        else [""] * 9
                    ),
                    *[answers.get(q, "") for q in questions],
                ]
            )
        return response


# --- jobs -----------------------------------------------------------------


class JobDetail(OmrView):
    def get(self, request, pk):
        jobs.reap_stale_jobs()
        job = get_object_or_404(ProcessJob, pk=pk, assessment__created_by=request.user)
        return Response(job_payload(job))


# --- sheets -----------------------------------------------------------------


class SheetDetail(OmrView):
    def get(self, request, pk):
        return Response(sheet_detail(self.owned_sheet(pk), request))

    def patch(self, request, pk):
        sheet = self.owned_sheet(pk)
        serializer = SheetRollSerializer(data=request.data)
        if not serializer.is_valid():
            return bad_request(first_error(serializer.errors))
        sheet.roll_no = serializer.validated_data["roll_no"].strip()
        # A teacher-entered roll number is authoritative.
        sheet.roll_confidence = "high" if sheet.roll_no else "none"
        sheet.save(update_fields=["roll_no", "roll_confidence", "updated_at"])
        return Response(sheet_detail(sheet, request))

    def delete(self, request, pk):
        sheet = self.owned_sheet(pk)
        if (
            sheet.status == "pending"
            and sheet.assessment.jobs.filter(status__in=jobs.ACTIVE_STATUSES).exists()
        ):
            return conflict("This sheet is still processing.")
        sheet.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SheetVerify(OmrView):
    """Human-in-the-loop: confirm/correct one question, then rescore."""

    def patch(self, request, pk):
        sheet = self.owned_sheet(pk)
        serializer = SheetVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return bad_request(first_error(serializer.errors))
        question_no = serializer.validated_data["question_no"]
        selected = serializer.validated_data["selected"]
        with transaction.atomic():
            # Lock: two reviewers verifying different questions on the same
            # sheet must not overwrite each other's per_question edits.
            result = SheetResult.objects.select_for_update().filter(sheet=sheet).first()
            if result is None:
                return bad_request("Sheet has not been processed yet.")
            entry = next(
                (q for q in result.per_question if q.get("q") == question_no), None
            )
            if entry is None:
                return bad_request(f"Question {question_no} not found on this sheet.")
            entry["verified"] = True
            entry["verified_selected"] = selected
            jobs.rescore_result(result, sheet.assessment)
        sheet.refresh_from_db()
        return Response(sheet_detail(sheet, request))
