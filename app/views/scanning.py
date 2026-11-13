from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from app.models.scanning import AnswerSheet, ScanBatch, StudentAnswer
from app.serializers.scanning import (
    AnswerSheetSerializer,
    ScanBatchSerializer,
    StudentAnswerSerializer,
)


class ScanBatchView(viewsets.ModelViewSet):
    serializer_class = ScanBatchSerializer
    http_method_names = ("get", "head", "options", "post", "delete")
    swagger_tags = ["app_scanning"]
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("exam", "status")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ScanBatch.objects.none()
        return ScanBatch.objects.filter(uploaded_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        batch = self.get_object()
        if batch.status != ScanBatch.Status.PENDING:
            return Response(
                {"detail": "Only pending batches can be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=("post",))
    def process(self, request, pk=None):
        from app.services.grading import validate_exam_template
        from app.tasks import process_batch_task

        batch = self.get_object()
        if batch.status != batch.Status.PENDING:
            return Response(
                {"detail": "Only pending batches can be processed."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            validate_exam_template(batch.exam)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        process_batch_task.delay(batch.pk)
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=("post",))
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
    swagger_tags = ["app_scanning"]
    http_method_names = ("get", "head", "options", "post")
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("batch", "status", "student")

    @action(detail=True, methods=("post",))
    def resolve(self, request, pk=None):
        from app.services.grading import resolve_sheet

        student_id = request.data.get("student")
        if not student_id:
            return Response(
                {"student": "A student is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            sheet = resolve_sheet(self.get_object().pk, student_id, request.user)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(AnswerSheetSerializer(sheet).data)

    @action(detail=True, methods=("post",))
    def keep(self, request, pk=None):
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
        return AnswerSheet.objects.filter(batch__uploaded_by=self.request.user)


class StudentAnswerView(viewsets.ModelViewSet):
    serializer_class = StudentAnswerSerializer
    swagger_tags = ["app_scanning"]
    http_method_names = ("get", "head", "options", "patch")
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("sheet", "question", "flag", "is_overridden")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return StudentAnswer.objects.none()
        return StudentAnswer.objects.filter(sheet__batch__uploaded_by=self.request.user)
