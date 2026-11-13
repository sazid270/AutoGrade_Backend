from django.db.models import ProtectedError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from app.models.exams import AnswerKey, Exam, Question
from app.serializers.exams import (
    AnswerKeySerializer,
    ExamSerializer,
    QuestionSerializer,
)


class ExamView(viewsets.ModelViewSet):
    serializer_class = ExamSerializer
    swagger_tags = ["app_exams"]
    filter_backends = (DjangoFilterBackend, filters.SearchFilter)
    filterset_fields = ("subject", "grade", "section", "date")
    search_fields = ("title", "subject")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Exam.objects.none()
        return Exam.objects.filter(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        exam = self.get_object()
        if exam.batches.exists():
            return Response(
                {"detail": "Exams cannot be deleted after scanning begins."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "This exam cannot be deleted because it has results."},
                status=status.HTTP_409_CONFLICT,
            )

    @action(detail=True, methods=("post",))
    def regrade(self, request, pk=None):
        from app.tasks import regrade_exam_task

        exam = self.get_object()
        task = regrade_exam_task.delay(exam.pk)
        return Response({"status": "queued", "task_id": task.id})


class QuestionView(viewsets.ModelViewSet):
    serializer_class = QuestionSerializer
    swagger_tags = ["app_questions"]
    filterset_fields = ("exam", "type")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Question.objects.none()
        return Question.objects.filter(exam__created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        question = self.get_object()
        if question.exam.batches.exists():
            return Response(
                {"detail": "Questions cannot be deleted after scanning begins."},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class AnswerKeyView(viewsets.ModelViewSet):
    serializer_class = AnswerKeySerializer
    swagger_tags = ["app_answer_keys"]
    filterset_fields = ("question__exam", "source", "needs_review")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AnswerKey.objects.none()
        return AnswerKey.objects.select_related("question").filter(
            question__exam__created_by=self.request.user
        )
