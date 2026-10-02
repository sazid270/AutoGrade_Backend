from django.core.management.base import BaseCommand
from django.utils import timezone

from authentication.models.models import EmailOtpModel


class Command(BaseCommand):
    help = "Delete all expired OTPs from the database."

    def handle(self, *args, **options):
        deleted, _ = EmailOtpModel.objects.filter(
            expires_at__lt=timezone.now()
        ).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired OTPs."))
