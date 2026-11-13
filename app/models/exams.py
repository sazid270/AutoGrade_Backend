from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from simple_history.models import HistoricalRecords

from autograde.mixins.models import TimeStampMixin


class Exam(TimeStampMixin):
    class Type(models.TextChoices):
        MCQ = "mcq", "MCQ"
        CQ = "cq", "CQ"

    title = models.CharField(max_length=255)
    subject = models.CharField(max_length=128)
    institution = models.CharField(max_length=255, blank=True, default="")
    subject_code = models.CharField(max_length=32, blank=True)
    set_code = models.CharField(max_length=32, blank=True)
    class_or_batch = models.CharField(max_length=64)
    section = models.CharField(max_length=32, blank=True)
    exam_year = models.CharField(max_length=16, blank=True)
    exam_date = models.DateField()
    exam_time = models.TimeField(blank=True, null=True)
    exam_type = models.CharField(max_length=3, choices=Type.choices, default=Type.MCQ)
    marks = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    question_image = models.ImageField(
        upload_to="questions/%Y/%m/%d", blank=True, null=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="exams"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("created_by", "title"), name="unique_exam_title_per_teacher"
            )
        ]

    def __str__(self):
        return self.title


class Question(models.Model):
    class Type(models.TextChoices):
        MCQ = "MCQ", "MCQ"
        CQ = "CQ", "CQ"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="questions")
    number = models.PositiveIntegerField()
    type = models.CharField(max_length=3, choices=Type.choices, default=Type.MCQ)
    marks = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    options_count = models.PositiveSmallIntegerField(
        default=4, validators=[MinValueValidator(2), MaxValueValidator(10)]
    )
    allow_multiple = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("exam", "number"), name="unique_question_number_per_exam"
            )
        ]
        ordering = ("number",)

    def __str__(self):
        return f"{self.exam.title} - Question {self.number}"


class AnswerKey(models.Model):
    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCANNED = "scanned", "Scanned"
        MANUAL_EDIT = "manual_edit", "Manual edit"

    question = models.OneToOneField(
        Question, on_delete=models.CASCADE, related_name="answer_key"
    )
    correct_options = models.JSONField(default=list)
    source = models.CharField(max_length=16, choices=Source.choices)
    confidence = models.FloatField(blank=True, null=True)
    needs_review = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answer_key_edits",
    )
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"Answer key for {self.question}"
