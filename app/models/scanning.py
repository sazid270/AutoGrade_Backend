from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models
from simple_history.models import HistoricalRecords

from app.models.exams import Exam, Question
from app.models.students import Student
from autograde.mixins.models import TimeStampMixin


class ExamScanConfiguration(TimeStampMixin):
    exam = models.OneToOneField(
        Exam,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="scan_configuration",
    )
    omr_template = models.JSONField(default=dict, blank=True)
    answer_key_image = models.ImageField(
        upload_to="answer-keys/%Y/%m/%d", blank=True, null=True
    )

    def __str__(self):
        return f"Scan configuration for {self.exam}"


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
        return f"Batch {self.pk} for {self.exam}"


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
    image = models.ImageField(
        upload_to="answer-sheets/%Y/%m/%d",
        validators=[FileExtensionValidator(allowed_extensions=("jpg", "jpeg", "png"))],
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answer_sheets",
    )
    detected_roll_no = models.CharField(max_length=32, blank=True)
    detected_registration_no = models.CharField(max_length=64, blank=True)
    detected_name = models.CharField(max_length=255, blank=True)
    detected_subject_code = models.CharField(max_length=32, blank=True)
    detected_set_code = models.CharField(max_length=32, blank=True)
    roll_confidence = models.FloatField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    error_message = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Sheet {self.pk} ({self.status})"


class StudentAnswer(models.Model):
    class Flag(models.TextChoices):
        OK = "ok", "OK"
        BLANK = "blank", "Blank"
        MULTIPLE = "multiple", "Multiple"
        LOW_CONFIDENCE = "low_confidence", "Low confidence"

    sheet = models.ForeignKey(
        AnswerSheet, on_delete=models.CASCADE, related_name="answers"
    )
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    selected_options = models.JSONField(default=list)
    fill_ratios = models.JSONField(default=list)
    is_correct = models.BooleanField(null=True)
    marks_awarded = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    flag = models.CharField(max_length=20, choices=Flag.choices, default=Flag.OK)
    confidence = models.FloatField(null=True, blank=True)
    is_overridden = models.BooleanField(default=False)
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("sheet", "question"), name="unique_answer_per_sheet_question"
            )
        ]

    def __str__(self):
        return f"{self.sheet} - Question {self.question_id}"
