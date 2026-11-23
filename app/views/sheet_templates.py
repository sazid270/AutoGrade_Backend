from copy import deepcopy

from django.db import transaction
from django.db.models import ProtectedError
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from app.models.sheet_templates import SheetTemplate
from app.serializers.sheet_templates import (
    SheetTemplateCreateSerializer,
    SheetTemplatePatchSerializer,
    SheetTemplateSerializer,
)
from app.services.ai_provider import (
    AIProviderError,
    ConfigurationError,
    SheetImageError,
    validate_sheet_image,
)
from app.services.sheet_template import (
    TemplateExtractionError,
    TemplateMismatch,
    extract_template,
)
from app.utils import omr


class SheetTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = SheetTemplateSerializer
    http_method_names = ["get", "post", "patch", "delete"]
    swagger_tags = ["app_sheet_templates"]
    throttle_scope = "sheet_template"

    def get_throttles(self):
        uses_ai = self.action == "create" or (
            self.action == "partial_update" and "image_id" in self.request.data
        )
        if uses_ai:
            return [*super().get_throttles(), ScopedRateThrottle()]
        return super().get_throttles()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SheetTemplate.objects.none()
        return (
            SheetTemplate.objects.filter(created_by=self.request.user)
            .select_related("reference_image")
            .order_by("-created_at")
        )

    @swagger_auto_schema(
        request_body=SheetTemplateCreateSerializer,
        responses={201: SheetTemplateSerializer},
        operation_description=(
            "Create a template from an image already uploaded through the image API. "
            "Pass that upload's ID as image_id."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = SheetTemplateCreateSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        image = serializer.validated_data["reference_image"]
        try:
            validate_sheet_image(image)
            structure = extract_template(image)
        except ConfigurationError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SheetImageError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except TemplateExtractionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except TemplateMismatch as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except AIProviderError as exc:
            return Response(
                {"detail": exc.detail},
                status=exc.status_code or status.HTTP_502_BAD_GATEWAY,
            )
        template = SheetTemplate.objects.create(
            name=serializer.validated_data["name"],
            reference_image=image,
            structure=structure,
            created_by=request.user,
        )
        return Response(
            SheetTemplateSerializer(
                template,
                context=self.get_serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @swagger_auto_schema(
        request_body=SheetTemplatePatchSerializer,
        responses={200: SheetTemplateSerializer},
    )
    def partial_update(self, request, *args, **kwargs):
        template = self.get_object()
        if template.batches.exists():
            return Response(
                {"detail": "A template used by a batch cannot be changed."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = SheetTemplatePatchSerializer(
            template,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        reference_image = values.get("reference_image")
        if reference_image is not None:
            try:
                validate_sheet_image(reference_image)
                structure = extract_template(reference_image)
            except ConfigurationError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            except SheetImageError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except (TemplateExtractionError, TemplateMismatch) as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            except AIProviderError as exc:
                return Response(
                    {"detail": exc.detail},
                    status=exc.status_code or status.HTTP_502_BAD_GATEWAY,
                )
        with transaction.atomic():
            template = SheetTemplate.objects.select_for_update().get(pk=template.pk)
            if template.batches.exists():
                return Response(
                    {"detail": "A template used by a batch cannot be changed."},
                    status=status.HTTP_409_CONFLICT,
                )
            if reference_image is not None:
                template.reference_image = reference_image
                template.structure = structure
            if "name" in values:
                template.name = values["name"]
            if "numbering" in values:
                template.structure = deepcopy(template.structure)
                template.structure["numbering"] = values["numbering"]
                answer_template = template.structure["answers"]
                order = omr.question_order(answer_template, values["numbering"])
                template.structure["options_per_question"] = [
                    len(answer_template["questions"][index]["bubbles"])
                    for index in order
                ]
            template.save()
        return Response(
            SheetTemplateSerializer(
                template,
                context=self.get_serializer_context(),
            ).data
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        template = SheetTemplate.objects.select_for_update().get(pk=template.pk)
        if template.batches.exists():
            return Response(
                {"detail": "The template is still in use."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "The template reference image is still in use."},
                status=status.HTTP_409_CONFLICT,
            )
