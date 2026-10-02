import io
import os

from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image, UnidentifiedImageError
from rest_framework import authentication, parsers, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt import authentication as jwt_authentication

from autograde.mixins.viewsets import BaseModelViewSet
from filesystem.models.models import FileModel, ImageModel
from filesystem.serializers.serializers import FileSerializer, ImageSerializer
from filesystem.utils import (
    delete_file_from_storage,
    get_storage_info,
    validate_storage_configuration,
)

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


# File views
class FileView(BaseModelViewSet):
    queryset = FileModel.objects.all()
    serializer_class = FileSerializer
    parser_classes = [parsers.MultiPartParser]
    authentication_classes = [
        jwt_authentication.JWTAuthentication,
        authentication.SessionAuthentication,
    ]
    permission_classes = [permissions.IsAuthenticated]
    swagger_tags = ["file_file"]

    def create(self, request, *args, **kwargs):
        file = request.FILES.get("file")
        if not file:
            return Response({"error": "No file provided."}, status=400)

        if file.size > MAX_FILE_SIZE:
            return Response({"error": "File too large. Max 20MB."}, status=400)

        return super().create(request, *args, **kwargs)

    def perform_update(self, serializer):
        # Get the old instance to delete the old file
        old_instance = self.get_object()
        old_file = old_instance.file

        # Save the new instance
        instance = serializer.save()

        if old_file:
            # Check if file was replaced or removed
            if not instance.file or (
                instance.file and old_file.name != instance.file.name
            ):
                delete_file_from_storage(old_file.name)

    def perform_destroy(self, instance):
        # Delete the file from storage
        if instance.file:
            delete_file_from_storage(instance.file.name)

        # Delete the instance
        instance.delete()

    # Get current storage configuration information
    @action(
        detail=False, methods=["get"], permission_classes=[permissions.IsAuthenticated]
    )
    def storage_info(self, request):
        storage_info = get_storage_info()
        validation = validate_storage_configuration()

        return Response(
            {"storage_info": storage_info, "validation": validation},
            status=status.HTTP_200_OK,
        )


# Image views
class ImageView(BaseModelViewSet):
    queryset = ImageModel.objects.all()
    serializer_class = ImageSerializer
    parser_classes = [parsers.MultiPartParser]
    authentication_classes = [
        jwt_authentication.JWTAuthentication,
        authentication.SessionAuthentication,
    ]
    permission_classes = [permissions.IsAuthenticated]
    swagger_tags = ["file_image"]

    def create(self, request, *args, **kwargs):
        file = request.FILES.get("image")
        if not file:
            return Response({"error": "No image provided."}, status=400)

        # 1. Size check
        if file.size > MAX_IMAGE_SIZE:
            return Response({"error": "Image too large. Max 5MB."}, status=400)

        # 2. Pillow validate + re-encode
        try:
            with Image.open(file) as img:
                img.verify()
            file.seek(0)
            with Image.open(file) as img:
                if getattr(img, "n_frames", 1) > 1:
                    return Response(
                        {"error": "Animated images not supported."}, status=400
                    )
                clean_img = img.convert("RGB")
                output = io.BytesIO()
                clean_img.save(output, format="WEBP", quality=85)
                output.seek(0)
        except (UnidentifiedImageError, OSError):
            return Response({"error": "Invalid image file."}, status=400)

        request.FILES["image"] = InMemoryUploadedFile(
            output,
            "image",
            f"{os.path.splitext(file.name)[0]}.webp",
            "image/webp",
            output.getbuffer().nbytes,
            None,
        )

        return super().create(request, *args, **kwargs)

    def perform_update(self, serializer):
        # Get the old instance to delete the old image
        old_instance = self.get_object()
        old_image = old_instance.image

        # Save the new instance
        instance = serializer.save()

        if old_image:
            # Check if image was replaced or removed
            if not instance.image or (
                instance.image and old_image.name != instance.image.name
            ):
                delete_file_from_storage(old_image.name)

    def perform_destroy(self, instance):
        # Delete the image from storage
        if instance.image:
            delete_file_from_storage(instance.image.name)

        # Delete the instance
        instance.delete()

    # Get current storage configuration information
    @action(
        detail=False, methods=["get"], permission_classes=[permissions.IsAuthenticated]
    )
    def storage_info(self, request):
        storage_info = get_storage_info()
        validation = validate_storage_configuration()

        return Response(
            {"storage_info": storage_info, "validation": validation},
            status=status.HTTP_200_OK,
        )
