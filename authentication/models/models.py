from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from autograde.mixins.models import TimeStampMixin


class EmailOtpModel(TimeStampMixin):
    email = models.EmailField(unique=True)
    otp_hash = models.CharField(max_length=64)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    last_sent_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    def __str__(self):
        return self.email

    def is_expired(self):
        return timezone.now() >= self.expires_at

    def seconds_until_resend(self):
        next_at = self.last_sent_at + timedelta(
            minutes=settings.OTP_RESEND_COOLDOWN_TIME
        )
        return max(0, int((next_at - timezone.now()).total_seconds()))
