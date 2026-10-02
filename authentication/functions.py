import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.db import IntegrityError
from django.template.loader import render_to_string
from rest_framework_simplejwt.tokens import RefreshToken

from authorization.choices import USER_TYPE_CHOICES

logger = logging.getLogger(__name__)
User = get_user_model()


def send_otp_email(email, otp):
    try:
        message = EmailMessage(
            subject="Your OTP for AutoGrade",
            body=render_to_string("authentication/email.html", {"otp": otp}),
            from_email=settings.EMAIL_HOST_USER,
            to=[email],
        )
        message.content_subtype = "html"
        message.send()
        return True
    except Exception:
        logger.exception("Failed to send OTP email")
        return False


def get_or_create_user(email, first_name=None, last_name=None):
    user = User.objects.filter(email__iexact=email).first()
    created = False
    if user is None:
        try:
            user = User.objects.create_user(
                email=email,
                password=None,
                user_type=USER_TYPE_CHOICES[1][0],
                verified=True,
                first_name=first_name,
                last_name=last_name,
            )
            created = True
        except IntegrityError:
            user = User.objects.get(email__iexact=email)

    if not user.verified:
        user.verified = True
        user.save(update_fields=["verified"])
    return user, created


def build_auth_response(user, created=False):
    refresh = RefreshToken.for_user(user)
    refresh["name"] = user.email
    refresh["id"] = str(user.id)
    refresh["user_type"] = user.user_type
    return {
        "refreshToken": str(refresh),
        "accessToken": str(refresh.access_token),
        "isNewUser": created,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "phone": user.phone,
            "user_type": user.user_type,
        },
    }
