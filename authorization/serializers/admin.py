from rest_framework import serializers

from authorization.generics.serializers import ProfileSerializer
from authorization.models.models import CustomUserModel


# Admin serializers
class AdminProfileSerializer(ProfileSerializer):
    class Meta:
        model = CustomUserModel
        fields = ProfileSerializer.Meta.fields
        read_only_fields = ProfileSerializer.Meta.read_only_fields


# Profile management serializers
class ProfileActivationSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUserModel
        fields = ("is_active",)
