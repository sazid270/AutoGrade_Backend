from django.conf import settings
from django.db import models

from app.models.exams import Exam
from app.models.students import Student
from autograde.mixins.models import TimeStampMixin
from filesystem.models.models import ImageModel


class ScanBatch(TimeStampMixin):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        DONE = "done", "Done"
        DONE_WITH_ERRORS = "done_with_errors", "Done with errors"
        FAILED = "failed", "Failed"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="batches")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    total_sheets = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    flagged_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Answer-sheet batch {self.pk} for {self.exam}"


class AnswerSheet(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"
        NEEDS_REVIEW = "needs_review", "Needs review"
        DISCARDED = "discarded", "Discarded"
        FAILED = "failed", "Failed"

    batch = models.ForeignKey(
        ScanBatch, on_delete=models.CASCADE, related_name="sheets"
    )
    image = models.ForeignKey(
        ImageModel,
        on_delete=models.PROTECT,
        related_name="answer_sheets",
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answer_sheets",
    )
    review_reasons = models.JSONField(default=list)
    scanned_mcq_marks = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    error_message = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Sheet {self.pk} ({self.status})"


class StudentAnswer(models.Model):
    class Source(models.TextChoices):
        EXTRACTED = "extracted", "Extracted"
        MANUAL_EDIT = "manual_edit", "Manual edit"

    sheet = models.ForeignKey(
        AnswerSheet, on_delete=models.CASCADE, related_name="answers"
    )
    question_number = models.PositiveIntegerField()
    selected_options = models.JSONField(default=list)
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.EXTRACTED
    )
    needs_review = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("question_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("sheet", "question_number"),
                name="unique_student_answer_per_sheet",
            )
        ]

    def __str__(self):
        return f"Sheet {self.sheet_id} - question {self.question_number}"
