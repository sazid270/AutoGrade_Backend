from django.urls import path
from rest_framework_simplejwt.views import (
    TokenBlacklistView,
    TokenRefreshView,
    TokenVerifyView,
)

from authentication.views.google import GoogleAuthView
from authentication.views.otp import OtpRequestView, OtpVerifyView

urlpatterns = [
    path("otp/request/", OtpRequestView.as_view(), name="otp_request"),
    path("otp/verify/", OtpVerifyView.as_view(), name="otp_verify"),
    path("google/", GoogleAuthView.as_view(), name="google_auth"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("token/blacklist/", TokenBlacklistView.as_view(), name="token_blacklist"),
]
