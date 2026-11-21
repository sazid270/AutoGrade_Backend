import logging

from celery import shared_task
from django.db import transaction

from app.models.scanning import AnswerSheet, ScanBatch
from app.services.ai_provider import ProviderTransientError
from app.services.grading import process_sheet

SHEET_PROVIDER_MAX_RETRIES = 5
logger = logging.getLogger(__name__)


def _record_sheet_completion(
    batch_id: int,
) -> None:
    with transaction.atomic():
        batch = ScanBatch.objects.select_for_update().get(pk=batch_id)
        sheets = batch.sheets.values("status")
        status_values = [item["status"] for item in sheets]
        terminal = {
            AnswerSheet.Status.PROCESSED,
            AnswerSheet.Status.NEEDS_REVIEW,
            AnswerSheet.Status.FAILED,
            AnswerSheet.Status.DISCARDED,
        }
        processed_count = sum(value in terminal for value in status_values)
        flagged_count = sum(
            value == AnswerSheet.Status.NEEDS_REVIEW for value in status_values
        )
        failed_count = sum(
            value == AnswerSheet.Status.FAILED for value in status_values
        )
        batch.processed_count = processed_count
        batch.flagged_count = flagged_count
        batch.failed_count = failed_count
        if processed_count >= batch.total_sheets:
            batch.status = (
                ScanBatch.Status.DONE_WITH_ERRORS
                if failed_count
                else ScanBatch.Status.DONE
            )
        batch.save(
            update_fields=(
                "processed_count",
                "flagged_count",
                "failed_count",
                "status",
            )
        )


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    rate_limit="15/m",
    autoretry_for=(ProviderTransientError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": SHEET_PROVIDER_MAX_RETRIES},
)
def process_sheet_task(self, sheet_id: int) -> int | None:
    try:
        sheet = AnswerSheet.objects.select_related("batch").get(pk=sheet_id)
    except AnswerSheet.DoesNotExist:
        logger.warning("Skipping sheet task for missing answer sheet %s.", sheet_id)
        return None
    if sheet.status in (
        AnswerSheet.Status.PROCESSED,
        AnswerSheet.Status.NEEDS_REVIEW,
        AnswerSheet.Status.FAILED,
    ):
        _record_sheet_completion(sheet.batch_id)
        return sheet.pk
    try:
        sheet = process_sheet(sheet_id)
    except ProviderTransientError as exc:
        if self.request.retries < SHEET_PROVIDER_MAX_RETRIES:
            raise
        batch_id = sheet.batch_id
        AnswerSheet.objects.filter(pk=sheet_id).update(
            status=AnswerSheet.Status.FAILED,
            error_message=str(exc),
            processed_at=None,
        )
        _record_sheet_completion(batch_id)
        return sheet_id
    except Exception as exc:
        batch_id = sheet.batch_id
        AnswerSheet.objects.filter(pk=sheet_id).update(
            status=AnswerSheet.Status.FAILED,
            error_message=str(exc),
            processed_at=None,
        )
        _record_sheet_completion(batch_id)
        return sheet_id
    _record_sheet_completion(sheet.batch_id)
    return sheet.pk


@shared_task(acks_late=True, reject_on_worker_lost=True)
def process_batch_task(batch_id: int) -> list[str]:
    try:
        with transaction.atomic():
            batch = ScanBatch.objects.select_for_update().get(pk=batch_id)
            if batch.status != ScanBatch.Status.PENDING:
                return []
            batch.status = ScanBatch.Status.PROCESSING
            batch.processed_count = 0
            batch.flagged_count = 0
            batch.failed_count = 0
            batch.save(
                update_fields=(
                    "status",
                    "processed_count",
                    "flagged_count",
                    "failed_count",
                )
            )
            sheet_ids = list(
                batch.sheets.filter(status=AnswerSheet.Status.PENDING).values_list(
                    "pk", flat=True
                )
            )
    except ScanBatch.DoesNotExist:
        logger.warning("Skipping task for missing scan batch %s.", batch_id)
        return []
    return [process_sheet_task.delay(sheet_id).id for sheet_id in sheet_ids]
