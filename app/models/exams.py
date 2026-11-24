from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from autograde.mixins.models import TimeStampMixin
from filesystem.models.models import ImageModel


# Exam models
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
    question_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of questions on this exam's question paper.",
    )
    mcq_mark = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Marks awarded for each correct MCQ answer.",
    )
    negative_marks = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0)],
        help_text="Marks deducted for each incorrect MCQ answer.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="exams"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("created_by", "title"), name="unique_exam_title_per_teacher"
            ),
            models.CheckConstraint(
                check=models.Q(exam_type="mcq") | models.Q(negative_marks=0),
                name="negative_marks_only_for_mcq",
            ),
        ]

    def __str__(self):
        return self.title


# Question models
class Question(models.Model):
    exam = models.OneToOneField(
        Exam,
        on_delete=models.CASCADE,
        related_name="question",
    )
    question_images = models.ManyToManyField(
        ImageModel,
        related_name="question_images",
        blank=True,
    )
    options_count = models.PositiveSmallIntegerField(
        default=4, validators=[MinValueValidator(2), MaxValueValidator(10)]
    )
    allow_multiple = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.exam.title} - Question paper"


# Answer models
class Answer(models.Model):
    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCANNED = "scanned", "Scanned"
        MANUAL_EDIT = "manual_edit", "Manual edit"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="answers")
    question_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    correct_options = models.JSONField(default=list)
    source = models.CharField(max_length=16, choices=Source.choices)
    confidence = models.FloatField(blank=True, null=True)
    needs_review = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answer_edits",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("exam", "question_number"),
                name="unique_answer_per_exam_question",
            ),
            models.CheckConstraint(
                check=models.Q(question_number__gte=1),
                name="answer_question_number_positive",
            ),
        ]

    def __str__(self):
        return f"Answer for {self.exam} - Question {self.question_number}"
