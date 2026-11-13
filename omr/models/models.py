from django.db import models

from autograde.mixins.models import AuthorWithTimeStampMixin, TimeStampMixin
from filesystem.models.models import FileModel


class Assessment(AuthorWithTimeStampMixin):
    """One exam being marked: metadata, the OMR answer key and mark policy."""

    title = models.CharField(max_length=200)
    subject = models.CharField(max_length=100, blank=True, default="")
    class_name = models.CharField(max_length=100, blank=True, default="")
    exam_date = models.CharField(max_length=64, blank=True, default="")
    assessment_type = models.CharField(
        max_length=16,
        choices=(
            ("Written", "Written"),
            ("OMR", "OMR"),
            ("Mixed", "Mixed"),
        ),
        default="OMR",
    )
    # {"1": "A", "2": "C", ...} — question number (string) -> option letter.
    answer_key = models.JSONField(default=dict)
    correct_marks = models.PositiveIntegerField(default=1)
    wrong_marks = models.IntegerField(default=0)
    blank_marks = models.IntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=(
            ("processing", "processing"),
            ("completed", "completed"),
            ("failed", "failed"),
        ),
        default="processing",
    )

    def __str__(self) -> str:
        return self.title


class Sheet(AuthorWithTimeStampMixin):
    """One uploaded OMR sheet; idempotent per (assessment, content hash)."""

    assessment = models.ForeignKey(
        Assessment, related_name="sheets", on_delete=models.CASCADE
    )
    file = models.ForeignKey(
        FileModel,
        related_name="sheets",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    content_hash = models.CharField(max_length=64, db_index=True)
    file_name = models.CharField(max_length=255, blank=True, default="")
    roll_no = models.CharField(max_length=64, blank=True, default="")
    roll_confidence = models.CharField(
        max_length=8,
        choices=(
            ("high", "high"),
            ("low", "low"),
            ("none", "none"),
        ),
        default="none",
    )
    status = models.CharField(
        max_length=12,
        choices=(
            ("pending", "pending"),
            ("processed", "processed"),
            ("rejected", "rejected"),
        ),
        default="pending",
    )
    reject_reason = models.CharField(max_length=255, blank=True, default="")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        # Re-uploading the same sheet for the same exam updates, never duplicates.
        unique_together = (("assessment", "content_hash"),)

    def __str__(self) -> str:
        return f"{self.assessment_id}:{self.roll_no or self.file_name}"


class SheetResult(TimeStampMixin):
    """Scoring outcome for one sheet; per_question carries the review payload."""

    sheet = models.OneToOneField(
        Sheet, related_name="result", on_delete=models.CASCADE
    )
    total = models.IntegerField(default=0)
    correct = models.PositiveIntegerField(default=0)
    wrong = models.PositiveIntegerField(default=0)
    blank = models.PositiveIntegerField(default=0)
    scored_questions = models.PositiveIntegerField(default=0)
    percentage = models.FloatField(default=0.0)
    grade = models.CharField(max_length=4, blank=True, default="")
    detected_questions = models.PositiveIntegerField(default=0)
    needs_review_count = models.PositiveIntegerField(default=0)
    verified = models.BooleanField(default=False)
    processing_ms = models.PositiveIntegerField(default=0)
    layout = models.JSONField(default=dict)
    per_question = models.JSONField(default=list)

    def __str__(self) -> str:
        return f"result:{self.sheet_id}"


class ProcessJob(TimeStampMixin):
    """Background run of the 9-stage pipeline over one batch of sheets."""

    assessment = models.ForeignKey(
        Assessment, related_name="jobs", on_delete=models.CASCADE
    )
    status = models.CharField(
        max_length=12,
        choices=(
            ("queued", "queued"),
            ("running", "running"),
            ("done", "done"),
            ("error", "error"),
        ),
        default="queued",
    )
    stage_index = models.PositiveIntegerField(default=0)
    stage_name = models.CharField(max_length=80, blank=True, default="")
    stages = models.JSONField(default=list)
    sheets_total = models.PositiveIntegerField(default=0)
    sheets_done = models.PositiveIntegerField(default=0)
    summary = models.JSONField(default=dict)
    error = models.CharField(max_length=255, blank=True, default="")

    def __str__(self) -> str:
        return f"job:{self.pk}:{self.status}"
