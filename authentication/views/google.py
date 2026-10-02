from django.conf import settings
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from authentication.functions import build_auth_response, get_or_create_user
from authentication.serializers.serializers import GoogleAuthSerializer
from autograde.mixins.viewsets import BaseCreateAPIView


class GoogleAuthView(BaseCreateAPIView):
    serializer_class = GoogleAuthSerializer
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "google_auth"
    swagger_tags = ["authentication"]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            info = id_token.verify_oauth2_token(
                serializer.validated_data["id_token"],
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
                clock_skew_in_seconds=60,
            )
        except ValueError:
            return Response(
                {"detail": "Invalid token."}, status=status.HTTP_400_BAD_REQUEST
            )

        email = (info.get("email") or "").strip().lower()
        if not email or not info.get("email_verified"):
            return Response(
                {"detail": "Google email is missing or not verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user, created = get_or_create_user(
            email, info.get("given_name"), info.get("family_name")
        )
        if not user.is_active:
            return Response(
                {"detail": "Your account has been frozen."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(build_auth_response(user, created), status=status.HTTP_200_OK)
