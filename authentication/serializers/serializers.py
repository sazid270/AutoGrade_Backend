from typing import Any, cast

from rest_framework import serializers
from rest_framework.exceptions import NotAcceptable
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer as BaseTokenObtainPairSerializer,
)
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from authorization.serializers.serializers import UserSerializer


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.strip().lower()


class OtpVerifySerializer(EmailSerializer):
    otp = serializers.CharField(min_length=6, max_length=6)


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField()


# Token Obtain Pair serializer
class TokenObtainPairSerializer(BaseTokenObtainPairSerializer):
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        data = super().validate(attrs)
        data = cast(dict[str, Any], data)
        user = UserSerializer(self.user).data
        data["refreshToken"] = str(data.pop("refresh"))
        data["accessToken"] = str(data.pop("access"))
        user_dict = cast(dict[str, Any], dict(user))
        user_obj = cast(Any, self.user)
        user_dict["id"] = str(getattr(user_obj, "id", ""))
        user_dict["user_type"] = getattr(user_obj, "user_type", None)
        data["user"] = user_dict
        return data

    @classmethod
    def get_token(cls, user):
        if not user.verified:
            raise NotAcceptable(detail="User not verified")
        token = super().get_token(user)
        token["name"] = user.email
        token["id"] = str(user.id)
        token["user_type"] = user.user_type
        return token


# Refresh Token serializer
class RefreshTokenSerializer(TokenRefreshSerializer):
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        data = super().validate(attrs)
        data = cast(dict[str, Any], data)
        data["refreshToken"] = str(data.pop("refresh"))
        data["accessToken"] = str(data.pop("access"))
        return data
