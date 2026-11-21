from django.db import transaction
from django.http import Http404
from django_filters.rest_framework import DjangoFilterBackend
from drf_yasg.utils import no_body, swagger_auto_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import JSONParser
from rest_framework.response import Response

from app.models.scanning import AnswerSheet, ScanBatch
from app.serializers.scanning import (
    AnswerSheetBatchSerializer,
    AnswerSheetSerializer,
    AssignStudentSerializer,
)


class AnswerSheetBatchView(viewsets.ModelViewSet):
    serializer_class = AnswerSheetBatchSerializer
    http_method_names = ["get", "post", "delete"]
    parser_classes = (JSONParser,)
    swagger_tags = ["app_answer_sheet"]
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("exam", "status")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ScanBatch.objects.none()
        return ScanBatch.objects.filter(uploaded_by=self.request.user)

    @swagger_auto_schema(tags=["app_answer_sheet_batch_status"])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(tags=["app_answer_sheet_batch_status"])
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=["app_answer_sheet"],
        operation_description=(
            "Create a batch using image IDs returned by the filesystem image-upload "
            "API. Provide a JSON array with one ID per answer sheet. The response is "
            "the batch record used to track processing."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            batch = self.get_object()
            try:
                batch = self.get_queryset().select_for_update().get(pk=batch.pk)
            except ScanBatch.DoesNotExist as exc:
                raise Http404 from exc
            if batch.status != ScanBatch.Status.PENDING:
                return Response(
                    {"detail": "Only pending batches can be deleted."},
                    status=status.HTTP_409_CONFLICT,
                )
            return super().destroy(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=["app_answer_sheet"],
        request_body=no_body,
    )
    @action(detail=True, methods=("post",), url_path="retry-failed")
    def retry_failed(self, request, pk=None):
        from app.tasks import process_sheet_task

        batch = self.get_object()
        if batch.status not in (
            batch.Status.FAILED,
            batch.Status.DONE_WITH_ERRORS,
        ):
            return Response(
                {"detail": "Only completed batches with failures can be retried."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            failed_sheets = list(batch.sheets.filter(status=AnswerSheet.Status.FAILED))
            batch.status = batch.Status.PROCESSING
            batch.processed_count = max(0, batch.processed_count - len(failed_sheets))
            batch.failed_count = max(0, batch.failed_count - len(failed_sheets))
            batch.save(update_fields=("status", "processed_count", "failed_count"))
            AnswerSheet.objects.filter(
                pk__in=[sheet.pk for sheet in failed_sheets]
            ).update(
                status=AnswerSheet.Status.PENDING,
                error_message="",
                processed_at=None,
            )
        task_ids = [process_sheet_task.delay(sheet.pk).id for sheet in failed_sheets]
        return Response({"status": "queued", "task_ids": task_ids})


class AnswerSheetView(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = AnswerSheetSerializer
    swagger_tags = ["app_answer_sheet"]
    http_method_names = ["get", "post", "patch"]
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("batch", "status", "student")

    @swagger_auto_schema(
        tags=["app_answer_sheet"],
        request_body=AssignStudentSerializer,
        operation_description=(
            "Confirm the sheet after reviewing answers. Omit student to keep the "
            "student already matched to it."
        ),
    )
    @action(detail=True, methods=("post",), url_path="assign-student")
    def assign_student(self, request, pk=None):
        from app.services.grading import resolve_sheet

        serializer = AssignStudentSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        student = serializer.validated_data.get("student")
        try:
            sheet = resolve_sheet(
                self.get_object().pk,
                student.pk if student else None,
                request.user,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(AnswerSheetSerializer(sheet).data)

    @swagger_auto_schema(
        tags=["app_answer_sheet"],
        operation_description=(
            "Keep this sheet when duplicate sheets were detected for the student."
        ),
    )
    @action(detail=True, methods=("post",), url_path="keep-sheet")
    def keep_sheet(self, request, pk=None):
        from app.services.grading import keep_sheet

        try:
            sheet = keep_sheet(self.get_object().pk, request.user)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(AnswerSheetSerializer(sheet).data)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AnswerSheet.objects.none()
        return (
            AnswerSheet.objects.filter(batch__uploaded_by=self.request.user)
            .select_related("image")
            .prefetch_related("answers")
        )
