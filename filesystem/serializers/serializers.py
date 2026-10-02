from rest_framework import serializers

from filesystem.models.models import FileModel, ImageModel
from filesystem.utils import get_file_url


# File serializers
class FileSerializer(serializers.ModelSerializer):
    file_path = serializers.SerializerMethodField()

    class Meta:
        model = FileModel
        fields = "__all__"

    def get_file_path(self, obj):
        if obj.file:
            return get_file_url(obj.file)
        return None


# Image serializers
class ImageSerializer(serializers.ModelSerializer):
    image_path = serializers.SerializerMethodField()
    image = serializers.FileField()

    class Meta:
        model = ImageModel
        fields = "__all__"

    def get_image_path(self, obj):
        if obj.image:
            return get_file_url(obj.image)
        return None
