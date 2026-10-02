from rest_framework import serializers

from authorization.models.models import CustomUserModel


# General serializers
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUserModel
        fields = ("id", "email", "phone")
