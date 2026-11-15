from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_yasg.utils import swagger_auto_schema
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from app.models.exams import AnswerKey, Exam, Question
from app.serializers.exams import (
    AnswerExtractionResponseSerializer,
    AnswerImageExtractionRequestSerializer,
    ExamAnswerSetSerializer,
    ExamAnswersResponseSerializer,
    ExamSerializer,
    QuestionSerializer,
)
from app.services.answer_extraction import (
    AnswerExtractionError,
    extract_answers_from_image,
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

    def get_serializer_class(self):
        if self.action == "answers":
            if self.request.method == "GET":
                return ExamAnswersResponseSerializer
            return ExamAnswerSetSerializer
        if self.action == "extract_answers":
            return AnswerImageExtractionRequestSerializer
        return super().get_serializer_class()

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

    @swagger_auto_schema(
        method="get",
        responses={200: ExamAnswersResponseSerializer},
    )
    @swagger_auto_schema(
        method="post",
        request_body=ExamAnswerSetSerializer,
        responses={200: ExamAnswersResponseSerializer},
    )
    @action(detail=True, methods=("get", "post"), url_path="answers")
    def answers(self, request, **kwargs):
        """List or replace the full set of teacher-provided MCQ answers."""
        exam = self._get_exam_for_action(request, kwargs)
        if exam.exam_type != Exam.Type.MCQ:
            return Response(
                {
                    "detail": "Teacher answers are currently supported for MCQ exams only."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if request.method == "GET":
            return Response(self._answers_data(exam))

        serializer = self.get_serializer(
            data=request.data,
            context={**self.get_serializer_context(), "exam": exam},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self._answers_data(exam))

    @swagger_auto_schema(
        method="post",
        request_body=AnswerImageExtractionRequestSerializer,
        responses={200: AnswerExtractionResponseSerializer},
    )
    @action(detail=True, methods=("post",), url_path="answers/extract")
    def extract_answers(self, request, **kwargs):
        exam = self._get_exam_for_action(request, kwargs)
        if exam.exam_type != Exam.Type.MCQ:
            return Response(
                {"detail": "Image answer extraction is available for MCQ exams only."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = serializer.validated_data["image_id"]
        try:
            extracted = extract_answers_from_image(image)
        except AnswerExtractionError as exc:
            return Response({"detail": exc.detail}, status=exc.status_code)

        answers = []
        invalid_answers = []
        seen_numbers = set()
        for index, item in enumerate(extracted):
            if not isinstance(item, dict):
                invalid_answers.append(
                    {"index": index, "reason": "Expected an object."}
                )
                continue
            number = item.get("question_number")
            options = item.get("correct_options")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                invalid_answers.append(
                    {"index": index, "reason": "Invalid question_number."}
                )
                continue
            if number in seen_numbers:
                invalid_answers.append(
                    {"question_number": number, "reason": "Duplicate answer."}
                )
                continue
            if not isinstance(options, list) or not all(
                isinstance(option, str) for option in options
            ):
                invalid_answers.append(
                    {"question_number": number, "reason": "Invalid options list."}
                )
                continue
            normalized_options = sorted({option.strip().upper() for option in options})
            if (
                not normalized_options
                or not all(
                    len(option) == 1 and "A" <= option <= "J"
                    for option in normalized_options
                )
            ):
                invalid_answers.append(
                    {
                        "question_number": number,
                        "reason": "Answer options must be letters A through J.",
                    }
                )
                continue
            seen_numbers.add(number)
            answers.append(
                {"question_number": number, "correct_options": normalized_options}
            )

        return Response(
            {
                "exam": exam.pk,
                "source": "image",
                "requires_review": True,
                "answers": sorted(
                    answers, key=lambda answer: answer["question_number"]
                ),
                "invalid_answers": invalid_answers,
            }
        )

    def _get_exam_for_action(self, request, kwargs):
        exam = get_object_or_404(
            self.filter_queryset(self.get_queryset()),
            pk=kwargs["pk"],
        )
        self.check_object_permissions(request, exam)
        return exam

    @staticmethod
    def _answers_data(exam):
        questions = list(exam.questions.order_by("pk"))
        keys = {
            key.question_id: key
            for key in AnswerKey.objects.filter(question__exam=exam).select_related(
                "question"
            )
        }
        return {
            "exam": exam.pk,
            "answers": [
                {
                    "question_number": number,
                    "correct_options": keys[question.pk].correct_options
                    if question.pk in keys
                    else [],
                    "source": keys[question.pk].source
                    if question.pk in keys
                    else None,
                    "needs_review": keys[question.pk].needs_review
                    if question.pk in keys
                    else False,
                }
                for number, question in enumerate(questions, start=1)
            ],
        }

    @staticmethod
    def _answer_key_preview_data(exam):
        from app.services.grading import validate_exam_template

        questions = []
        for number, question in enumerate(
            exam.questions.prefetch_related("answer_key").order_by("pk"),
            start=1,
        ):
            answer_key = getattr(question, "answer_key", None)
            question_data = {
                "id": question.pk,
                "question_number": number,
                "question_images": [
                    image.image.url
                    for image in question.question_images.all()
                    if image.image
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
        return {
            "exam": exam.pk,
            "ready_for_scanning": readiness_error is None,
            "readiness_error": readiness_error,
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

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        question = self.get_object()
        exam = Exam.objects.select_for_update().get(pk=question.exam_id)
        if exam.batches.exists():
            return Response(
                {"detail": "Questions cannot be deleted after scanning begins."},
                status=status.HTTP_409_CONFLICT,
            )
        if AnswerKey.objects.filter(question=question).exists():
            return Response(
                {"detail": "Questions with saved answers cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)
