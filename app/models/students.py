from django.conf import settings
from django.db import models

from autograde.mixins.models import TimeStampMixin


class Student(TimeStampMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        NEEDS_COMPLETION = "needs_completion", "Needs completion"
        INACTIVE = "inactive", "Inactive"

    class CreatedVia(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCAN = "scan", "Scan"

    institution = models.CharField(max_length=255)
    roll_no = models.CharField(max_length=32)
    name = models.CharField(max_length=255, blank=True)
    grade = models.CharField(max_length=32, blank=True)
    class_name = models.CharField(max_length=64, blank=True)
    section = models.CharField(max_length=32, blank=True)
    academic_year = models.CharField(max_length=16, blank=True)
    guardian_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    gender = models.CharField(max_length=32, blank=True)
    date_of_birth = models.DateField(blank=True, null=True)
    photo = models.ImageField(upload_to="students/%Y/%m/%d", blank=True)
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.ACTIVE
    )
    created_via = models.CharField(
        max_length=16, choices=CreatedVia.choices, default=CreatedVia.MANUAL
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="students",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "created_by",
                    "institution",
                    "grade",
                    "section",
                    "academic_year",
                    "roll_no",
                ),
                name="unique_student_roll_in_context",
            )
        ]
        ordering = ("roll_no", "name")

    def __str__(self):
        return f"{self.roll_no} - {self.name or 'Unnamed student'}"
