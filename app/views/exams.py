from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404
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
    http_method_names = ["get", "post", "patch", "delete"]
    swagger_tags = ["app_exams"]
    filter_backends = (DjangoFilterBackend, filters.SearchFilter)
    filterset_fields = (
        "subject",
        "class_or_batch",
        "section",
        "exam_year",
        "exam_date",
        "exam_type",
    )
    search_fields = ("title", "subject", "subject_code")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Exam.objects.none()
        return Exam.objects.filter(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            exam = self.get_object()
            exam = get_object_or_404(
                self.filter_queryset(self.get_queryset()).select_for_update(),
                pk=exam.pk,
            )
            self.check_object_permissions(request, exam)
            if (
                exam.questions.exists()
                or exam.batches.exists()
                or exam.results.exists()
            ):
                return Response(
                    {
                        "detail": (
                            "An exam with questions, scan batches, or results "
                            "cannot be deleted."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            try:
                return super().destroy(request, *args, **kwargs)
            except ProtectedError:
                return Response(
                    {
                        "detail": (
                            "This exam cannot be deleted because related records "
                            "depend on it."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

    @action(detail=True, methods=("get",), url_path="answer-key-preview")
    def answer_key_preview(self, request, **kwargs):
        exam = self._get_exam_for_action(request, kwargs)
        return Response(self._answer_key_preview_data(exam))

    def _get_exam_for_action(self, request, kwargs):
        exam = get_object_or_404(
            self.filter_queryset(self.get_queryset()),
            pk=kwargs["pk"],
        )
        self.check_object_permissions(request, exam)
        return exam

    @staticmethod
    def _answer_key_preview_data(exam):
        from app.services.grading import validate_exam_template

        questions = []
        for question in exam.questions.prefetch_related(
            "question_images", "answer_key"
        ):
            answer_key = getattr(question, "answer_key", None)
            question_data = {
                "id": question.pk,
                "question_images": [
                    image.image.url for image in question.question_images.all()
                ],
                "correct_options": answer_key.correct_options if answer_key else [],
                "source": answer_key.source if answer_key else None,
                "needs_review": answer_key.needs_review if answer_key else False,
            }
            if exam.exam_type == Exam.Type.MCQ:
                question_data["options_count"] = question.options_count
                question_data["allow_multiple"] = question.allow_multiple
            questions.append(question_data)
        readiness_error = None
        try:
            validate_exam_template(exam)
        except ValueError as exc:
            readiness_error = str(exc)
        scan_configuration = getattr(exam, "scan_configuration", None)
        return {
            "exam": exam.pk,
            "ready_for_scanning": readiness_error is None,
            "readiness_error": readiness_error,
            "answer_key_image": scan_configuration.answer_key_image.url
            if scan_configuration and scan_configuration.answer_key_image
            else None,
            "questions": questions,
        }


class QuestionView(viewsets.ModelViewSet):
    serializer_class = QuestionSerializer
    http_method_names = ["get", "post", "patch", "delete"]
    swagger_tags = ["app_questions"]
    filterset_fields = ("exam",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Question.objects.none()
        return Question.objects.filter(
            exam__created_by=self.request.user
        ).prefetch_related("question_images")

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
    http_method_names = ["get", "post", "patch", "delete"]
    swagger_tags = ["app_answer_keys"]
    filterset_fields = ("question__exam", "source", "needs_review")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AnswerKey.objects.none()
        return AnswerKey.objects.select_related("question").filter(
            question__exam__created_by=self.request.user
        )
