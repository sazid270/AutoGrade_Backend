from decimal import Decimal

from django.conf import settings
from django.db import models

from app.models.exams import Exam
from app.models.scanning import AnswerSheet
from app.models.students import Student
from autograde.mixins.models import TimeStampMixin


# Result models
class Result(TimeStampMixin):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="results")
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="results"
    )
    sheet = models.ForeignKey(
        AnswerSheet,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="result",
    )
    mcq_marks = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    cq_marks = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True
    )
    total_marks = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    is_overridden = models.BooleanField(default=False)
    finalized = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("exam", "student"), name="unique_result_per_student"
            )
        ]

    def __str__(self):
        return f"{self.exam} - {self.student}"

    def recalculate(self):
        self.total_marks = self.mcq_marks + (self.cq_marks or Decimal("0"))
        return self
