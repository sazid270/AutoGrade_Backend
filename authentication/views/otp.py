from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from authentication.functions import (
    build_auth_response,
    get_or_create_user,
    send_otp_email,
)
from authentication.models.models import EmailOtpModel
from authentication.serializers.serializers import (
    EmailSerializer,
    OtpVerifySerializer,
)
from authentication.utils import generate_otp, hash_otp
from autograde.mixins.viewsets import BaseCreateAPIView


class OtpRequestView(BaseCreateAPIView):
    serializer_class = EmailSerializer
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_request"
    swagger_tags = ["authentication"]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        otp = generate_otp()
        with transaction.atomic():
            record = (
                EmailOtpModel.objects.select_for_update().filter(email=email).first()
            )
            if record and record.seconds_until_resend() > 0:
                wait = record.seconds_until_resend()
                return Response(
                    {
                        "detail": f"Please wait {wait} seconds before requesting a new OTP.",
                        "retry_after_seconds": wait,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            now = timezone.now()
            EmailOtpModel.objects.update_or_create(
                email=email,
                defaults={
                    "otp_hash": hash_otp(email, otp),
                    "failed_attempts": 0,
                    "last_sent_at": now,
                    "expires_at": now
                    + timedelta(minutes=settings.OTP_VERIFICATION_TIME),
                },
            )

        if not send_otp_email(email, otp):
            EmailOtpModel.objects.filter(email=email).delete()
            return Response(
                {"detail": "Could not send the email. Please try again."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "detail": "An OTP has been sent to your email.",
                "cooldown_seconds": settings.OTP_RESEND_COOLDOWN_TIME * 60,
            },
            status=status.HTTP_200_OK,
        )


class OtpVerifyView(BaseCreateAPIView):
    serializer_class = OtpVerifySerializer
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_verify"
    swagger_tags = ["authentication"]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        otp = serializer.validated_data["otp"]

        invalid = Response(
            {"detail": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST
        )

        with transaction.atomic():
            record = (
                EmailOtpModel.objects.select_for_update().filter(email=email).first()
            )
            if record is None:
                return invalid
            if record.is_expired():
                record.delete()
                return invalid

            if not constant_time_compare(record.otp_hash, hash_otp(email, otp)):
                record.failed_attempts += 1
                if record.failed_attempts >= settings.OTP_MAX_ATTEMPTS:
                    record.delete()
                else:
                    record.save(update_fields=["failed_attempts"])
                return invalid

            record.delete()

        user, created = get_or_create_user(email)
        if not user.is_active:
            return Response(
                {"detail": "Your account has been frozen."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(build_auth_response(user, created), status=status.HTTP_200_OK)
