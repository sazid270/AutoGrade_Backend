from django.conf import settings
from django.db import models

from autograde.mixins.models import TimeStampMixin
from filesystem.models.models import ImageModel


class SheetTemplate(TimeStampMixin):
    name = models.CharField(max_length=255)
    structure = models.JSONField()
    reference_image = models.ForeignKey(
        ImageModel,
        on_delete=models.PROTECT,
        related_name="sheet_templates",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sheet_templates",
    )

    def __str__(self):
        return self.name
