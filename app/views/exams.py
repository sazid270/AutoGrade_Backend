from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_yasg.utils import swagger_auto_schema
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from app.models.exams import Answer, Exam, Question
from app.serializers.exams import (
    AnswerExtractionResponseSerializer,
    AnswerImageExtractionRequestSerializer,
    AnswerPatchSerializer,
    ExamAnswerSetSerializer,
    ExamAnswersResponseSerializer,
    ExamSerializer,
    QuestionSerializer,
    SavedAnswerSerializer,
)
from app.services.answer_extraction import (
    AnswerExtractionError,
    extract_answers_from_image,
)
from app.services.answers import ensure_exam_questions


# Exam views
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
        if self.action == "answer_detail":
            return AnswerPatchSerializer
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
                or exam.answers.exists()
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
        """List or upsert supplied MCQ answers without deleting omitted entries."""
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
        method="patch",
        request_body=AnswerPatchSerializer,
        responses={200: SavedAnswerSerializer},
    )
    @action(
        detail=True,
        methods=("patch",),
        url_path=r"answers/(?P<question_number>\d+)",
    )
    def answer_detail(self, request, question_number, **kwargs):
        exam = self._get_exam_for_action(request, kwargs)
        if exam.exam_type != Exam.Type.MCQ:
            return Response(
                {
                    "detail": "Teacher answers are currently supported for MCQ exams only."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        answer = get_object_or_404(
            Answer.objects.filter(exam=exam),
            question_number=question_number,
        )
        serializer = self.get_serializer(
            answer,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        answer = serializer.save()
        return Response(SavedAnswerSerializer(answer).data)

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
            if not normalized_options or not all(
                len(option) == 1 and "A" <= option <= "J"
                for option in normalized_options
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

        saved_answers = []
        with transaction.atomic():
            exam = Exam.objects.select_for_update().get(pk=exam.pk)
            questions = ensure_exam_questions(
                exam,
                {
                    answer["question_number"]: answer["correct_options"]
                    for answer in answers
                },
            )
            for extracted_answer in sorted(
                answers, key=lambda item: item["question_number"]
            ):
                number = extracted_answer["question_number"]
                question = questions[number - 1]
                answer = (
                    Answer.objects.select_for_update()
                    .filter(exam=exam, question_number=number)
                    .first()
                )
                if answer is not None:
                    answer.question = question
                    answer.correct_options = extracted_answer["correct_options"]
                    answer.source = Answer.Source.SCANNED
                    answer.confidence = None
                    answer.needs_review = True
                    answer.updated_by = request.user
                    answer.save(
                        update_fields=(
                            "question",
                            "correct_options",
                            "source",
                            "confidence",
                            "needs_review",
                            "updated_by",
                            "updated_at",
                        )
                    )
                else:
                    answer = Answer.objects.create(
                        exam=exam,
                        question_number=number,
                        question=question,
                        correct_options=extracted_answer["correct_options"],
                        source=Answer.Source.SCANNED,
                        needs_review=True,
                        updated_by=request.user,
                    )
                saved_answers.append(answer)

        return Response(
            {
                "exam": exam.pk,
                "source": "image",
                "requires_review": bool(invalid_answers)
                or any(answer.needs_review for answer in saved_answers),
                "answers": SavedAnswerSerializer(saved_answers, many=True).data,
                "conflicts": [],
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
        answers = Answer.objects.filter(exam=exam).order_by("question_number")
        return {
            "exam": exam.pk,
            "answers": SavedAnswerSerializer(answers, many=True).data,
        }


# Question views
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
        if Answer.objects.filter(question=question).exists():
            return Response(
                {"detail": "Questions with saved answers cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)
