"""Background runner for the 9-stage OMR pipeline.

A job walks every *pending* sheet of one assessment through the stages,
saving progress after each stage so the app can poll ``/jobs/{id}/``.

Jobs run on a daemon thread inside the web process. That keeps deployment
simple (no broker), at the cost that a server restart kills running jobs;
``reap_stale_jobs`` turns those into ``error`` so the teacher can hit
*reprocess* instead of watching a spinner forever.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from omr import pipeline
from omr.models import Assessment, ProcessJob, Sheet, SheetResult

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
# A running job saves at least once per sheet per stage; silence this long
# means its thread is gone (server restart, worker recycled, OOM).
STALE_AFTER = timedelta(seconds=getattr(settings, "OMR_JOB_STALE_SECONDS", 10 * 60))


def _threaded() -> bool:
    """False (tests) runs jobs synchronously inside the request."""
    return getattr(settings, "OMR_RUN_JOBS_IN_THREAD", True)


class JobAlreadyRunning(Exception):
    """Another job is still processing this assessment."""


def initial_stages() -> list[dict]:
    return [
        {"index": index, "name": name, "status": "pending", "ms": 0, "detail": ""}
        for index, name in pipeline.STAGES
    ]


def reap_stale_jobs(assessment: Assessment | None = None) -> int:
    """Mark jobs whose worker died as failed; returns how many."""
    cutoff = timezone.now() - STALE_AFTER
    stale = ProcessJob.objects.filter(status__in=ACTIVE_STATUSES, updated_at__lt=cutoff)
    if assessment is not None:
        stale = stale.filter(assessment=assessment)
    assessment_ids = list(stale.values_list("assessment_id", flat=True))
    count = stale.update(
        status="error",
        error="Processing was interrupted (server restart?) — reprocess the sheets.",
    )
    if assessment_ids:
        Assessment.objects.filter(pk__in=assessment_ids, status="processing").update(
            status="failed"
        )
    return count


def start_job(assessment: Assessment) -> ProcessJob:
    """Queue a job for the assessment's pending sheets and launch it.

    Raises JobAlreadyRunning if one is already active for the assessment.
    """
    reap_stale_jobs(assessment)
    with transaction.atomic():
        # Row lock serialises concurrent "add sheets"/"reprocess" requests.
        Assessment.objects.select_for_update().get(pk=assessment.pk)
        if ProcessJob.objects.filter(
            assessment=assessment, status__in=ACTIVE_STATUSES
        ).exists():
            raise JobAlreadyRunning
        pending = assessment.sheets.filter(status="pending").count()
        job = ProcessJob.objects.create(
            assessment=assessment,
            status="queued",
            sheets_total=pending,
            stages=initial_stages(),
        )
        Assessment.objects.filter(pk=assessment.pk).update(
            status="processing", updated_at=timezone.now()
        )
    if _threaded():
        transaction.on_commit(lambda: _spawn(job.pk))
    else:
        run_job(job.pk)
        job.refresh_from_db()
    return job


def _spawn(job_id: int) -> None:
    threading.Thread(
        target=run_job, args=(job_id,), daemon=True, name=f"omr-job-{job_id}"
    ).start()


def run_job(job_id: int) -> None:
    """Thread entry point; never raises."""
    threaded = _threaded()
    if threaded:
        close_old_connections()
    try:
        _run(job_id)
    except Exception as exc:  # noqa: BLE001 — a job must never die silently
        logger.exception("OMR job %s failed", job_id)
        job = ProcessJob.objects.filter(pk=job_id).first()
        if job is not None:
            ProcessJob.objects.filter(pk=job_id).update(
                status="error", error=str(exc)[:250], updated_at=timezone.now()
            )
            Assessment.objects.filter(pk=job.assessment_id).update(status="failed")
    finally:
        if threaded:
            connection.close()


def finished_status(assessment: Assessment) -> str:
    """Assessment status once no job is running."""
    sheets = assessment.sheets.all()
    if sheets.filter(status="processed").exists():
        return "completed"
    return "failed" if sheets.exists() else "completed"


def _run(job_id: int) -> None:
    job = ProcessJob.objects.select_related("assessment").get(pk=job_id)
    assessment = job.assessment
    key = {str(k): str(v).upper() for k, v in assessment.answer_key.items()}
    policy = (assessment.correct_marks, assessment.wrong_marks, assessment.blank_marks)
    expected_options = pipeline.expected_option_count(key)

    sheets = list(assessment.sheets.filter(status="pending").order_by("id"))
    job.status = "running"
    job.sheets_total = len(sheets)
    job.stages = job.stages or initial_stages()
    job.save(update_fields=["status", "sheets_total", "stages", "updated_at"])

    rejected: list[Sheet] = []

    def begin(index: int) -> float:
        job.stages[index - 1]["status"] = "running"
        job.stage_index = index
        job.stage_name = pipeline.STAGES[index - 1][1]
        job.save(update_fields=["stages", "stage_index", "stage_name", "updated_at"])
        return time.monotonic()

    def finish(index: int, detail: str, started: float) -> None:
        job.stages[index - 1].update(
            status="done",
            ms=int((time.monotonic() - started) * 1000),
            detail=detail,
        )
        job.save(update_fields=["stages", "updated_at"])

    def heartbeat() -> None:
        # Keeps updated_at fresh on long stages so the reaper leaves us alone.
        job.save(update_fields=["updated_at"])

    def reject(sheet: Sheet, reason: str) -> None:
        sheet.status = "rejected"
        sheet.reject_reason = reason[:250]
        sheet.save(update_fields=["status", "reject_reason", "updated_at"])
        SheetResult.objects.filter(sheet=sheet).delete()
        rejected.append(sheet)

    def guarded(item: dict, label: str, fn) -> bool:
        """Run one sheet's stage step; reject the sheet on failure."""
        try:
            fn(item)
            return True
        except pipeline.SheetRejected as exc:
            reject(item["sheet"], str(exc))
        except Exception as exc:  # noqa: BLE001 — one bad sheet != failed job
            logger.exception("%s failed for sheet %s", label, item["sheet"].pk)
            reject(item["sheet"], f"{label} failed — {exc}")
        return False

    # --- stage 1: upload / scan (files were uploaded by the app) ---------
    started = begin(1)
    finish(1, f"{len(sheets)} sheet(s) queued from the upload step", started)

    # --- stage 2: validation & preprocessing -----------------------------
    started = begin(2)
    work: list[dict] = []
    for sheet in sheets:
        item = {"sheet": sheet}

        def validate(item: dict) -> None:
            img = pipeline.decode_image(_read_file(item["sheet"]))
            gray = pipeline.preprocess(img)
            pipeline.check_blur(gray)
            item["gray"] = gray

        if guarded(item, "Image validation", validate):
            work.append(item)
        heartbeat()
    finish(2, f"{len(work)} preprocessed, {len(rejected)} rejected", started)

    # --- stage 3: registration (page warp, deskew, scale) ----------------
    started = begin(3)
    warped = 0

    def align(item: dict) -> None:
        # Flatten the page first, then fine-tune rotation: deskewing a photo
        # first paints white borders that hide the page outline.
        gray, info = pipeline.register(item["gray"])
        gray, angle = pipeline.deskew(gray)
        info["angle"] = angle
        item["gray"] = pipeline.normalize_scale(gray)
        item["registration"] = info

    work = [item for item in work if guarded(item, "Sheet alignment", align)]
    warped = sum(1 for item in work if item["registration"]["warped"])
    finish(3, f"page warped on {warped} sheet(s), rest already flat", started)

    # --- stage 4: field detection (ROI) + roll number --------------------
    started = begin(4)

    def locate(item: dict) -> None:
        item["layout"] = pipeline.detect_layout(item["gray"], expected_options)
        item["roll_no"], item["roll_confidence"] = pipeline.read_roll_no(
            item["gray"], item["layout"]["roll_block"]
        )

    work = [item for item in work if guarded(item, "Layout detection", locate)]
    roll_read = sum(1 for item in work if item["roll_confidence"] != "none")
    finish(
        4,
        f"grids located on {len(work)} sheet(s); roll no. read on {roll_read}",
        started,
    )

    # --- stage 5: bubble recognition -------------------------------------
    started = begin(5)

    def read(item: dict) -> None:
        item["records"] = pipeline.recognize(item["gray"], item["layout"])

    work = [item for item in work if guarded(item, "Bubble recognition", read)]
    finish(5, f"answers read on {len(work)} sheet(s)", started)

    # --- stage 6: ambiguity & confidence ---------------------------------
    started = begin(6)

    def review(item: dict) -> None:
        item["needs"] = pipeline.flag_questions(
            item["gray"], item["records"], key, expected_options
        )

    work = [item for item in work if guarded(item, "Confidence check", review)]
    flagged_total = sum(item["needs"] for item in work)
    finish(6, f"{flagged_total} question(s) flagged for human review", started)

    # --- stage 7: answer matching & scoring ------------------------------
    started = begin(7)
    for item in work:
        item["totals"] = pipeline.score_records(item["records"], key, *policy)
    finish(7, f"scored {len(work)} sheet(s) against the key", started)

    # --- stage 8: result generation --------------------------------------
    started = begin(8)
    for item in work:
        layout = item["layout"]
        item["layout_payload"] = {
            "blocks": layout["blocks"],
            "questions_detected": len(layout["questions"]),
            "bubbles_found": layout["bubbles_found"],
            "note": layout["layout_note"],
            "registration": item["registration"],
        }
    finish(8, f"results generated for {len(work)} sheet(s)", started)

    # --- stage 9: storage & analytics ------------------------------------
    started = begin(9)
    percentages: list[float] = []
    for done, item in enumerate(work, start=1):
        _store(item)
        percentages.append(item["totals"]["percentage"])
        job.sheets_done = len(rejected) + done
        job.save(update_fields=["sheets_done", "updated_at"])

    job.summary = {
        "processed": len(work),
        "rejected": len(rejected),
        "needs_review": flagged_total,
        "average": round(sum(percentages) / len(percentages), 1)
        if percentages
        else 0.0,
    }
    finish(9, f"saved {len(work)} result(s); analytics ready", started)
    job.sheets_done = job.sheets_total
    job.status = "done"
    job.save(update_fields=["summary", "status", "sheets_done", "updated_at"])
    assessment.status = finished_status(assessment)
    assessment.save(update_fields=["status", "updated_at"])


def _store(item: dict) -> None:
    sheet: Sheet = item["sheet"]
    totals = item["totals"]
    gray = item["gray"]
    with transaction.atomic():
        sheet.roll_no = item.get("roll_no") or ""
        sheet.roll_confidence = item.get("roll_confidence", "none")
        sheet.status = "processed"
        sheet.reject_reason = ""
        sheet.width = int(gray.shape[1])
        sheet.height = int(gray.shape[0])
        sheet.save(
            update_fields=[
                "roll_no",
                "roll_confidence",
                "status",
                "reject_reason",
                "width",
                "height",
                "updated_at",
            ]
        )
        SheetResult.objects.update_or_create(
            sheet=sheet,
            defaults={
                **_totals_fields(totals),
                "detected_questions": len(item["layout"]["questions"]),
                "needs_review_count": item["needs"],
                "verified": item["needs"] == 0,
                "layout": item["layout_payload"],
                "per_question": item["records"],
            },
        )


def _totals_fields(totals: dict) -> dict:
    return {
        "total": totals["total"],
        "correct": totals["correct"],
        "wrong": totals["wrong"],
        "blank": totals["blank"],
        "scored_questions": totals["scored_questions"],
        "percentage": totals["percentage"],
        "grade": totals["grade"],
    }


def rescore_result(result: SheetResult, assessment: Assessment) -> None:
    """Re-apply the (possibly edited) key and mark policy to stored answers.

    No image work: per_question keeps what was read and what the teacher
    verified, so a key fix rescores the whole class instantly.
    """
    key = {str(k): str(v).upper() for k, v in assessment.answer_key.items()}
    records = result.per_question
    pipeline.add_missing_questions(records, key)
    keyed = {int(q) for q in key}
    for record in records:
        if record.get("verified"):
            record["flags"] = []
        elif record["q"] in keyed:
            record["flags"] = list(record.get("raw_flags", record.get("flags", [])))
        else:
            record["flags"] = []
        record["flagged"] = bool(record["flags"])
    totals = pipeline.score_records(
        records,
        key,
        assessment.correct_marks,
        assessment.wrong_marks,
        assessment.blank_marks,
    )
    for field, value in _totals_fields(totals).items():
        setattr(result, field, value)
    result.per_question = records
    result.needs_review_count = sum(1 for r in records if r["flagged"])
    result.verified = result.needs_review_count == 0
    result.save()


def _read_file(sheet: Sheet) -> bytes:
    if sheet.file is None or not sheet.file.file:
        raise pipeline.SheetRejected("Original upload is missing on the server.")
    try:
        with sheet.file.file.open("rb") as handle:
            return handle.read()
    except (FileNotFoundError, OSError) as exc:
        raise pipeline.SheetRejected(
            "Original upload is missing on the server."
        ) from exc
