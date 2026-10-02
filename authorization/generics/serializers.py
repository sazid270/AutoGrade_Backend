from rest_framework import serializers

from authorization.models.models import CustomUserModel
from filesystem.models.models import ImageModel


class ProfileSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomUserModel
        fields = (
            "id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "date_of_birth",
            "gender",
            "user_type",
            "verified",
            "is_active",
            "date_joined",
            "image",
            "image_url",
        )
        read_only_fields = (
            "id",
            "email",
            "user_type",
            "verified",
            "date_joined",
            "is_active",
            "image_url",
        )

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        if instance and instance.phone and ("phone" in attrs) and not attrs["phone"]:
            raise serializers.ValidationError(
                "Phone number cannot be removed once set."
            )
        return attrs

    def get_image_url(self, obj):
        image_obj = None
        if hasattr(obj, "image") and obj.image:
            if hasattr(obj.image, "image"):
                image_obj = obj.image
            else:
                try:
                    image_obj = ImageModel.objects.get(pk=obj.image)
                except ImageModel.DoesNotExist:
                    image_obj = None
        if image_obj and hasattr(image_obj, "image"):
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(image_obj.image.url)
            return image_obj.image.url
        return None
