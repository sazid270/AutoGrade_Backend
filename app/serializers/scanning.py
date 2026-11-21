from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import APIException

from app.models.scanning import AnswerSheet, ScanBatch, StudentAnswer
from app.models.students import Student
from app.services.ai_provider import (
    ConfigurationError,
    validate_sheet_reading_config,
)
from app.services.grading import validate_exam_ready
from app.utils.options import normalize_options
from filesystem.models.models import ImageModel


class SheetReadingUnavailable(APIException):
    status_code = 503
    default_code = "sheet_reading_unavailable"


class AnswerSheetBatchSerializer(serializers.ModelSerializer):
    image_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        write_only=True,
        allow_empty=False,
        help_text="Image IDs returned by POST /api/file/image/, one per sheet.",
    )

    class Meta:
        model = ScanBatch
        fields = "__all__"
        read_only_fields = (
            "uploaded_by",
            "status",
            "total_sheets",
            "processed_count",
            "flagged_count",
            "failed_count",
            "created_at",
        )

    def validate_exam(self, value):
        if value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError("You cannot use another user's exam.")
        try:
            validate_exam_ready(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        try:
            validate_sheet_reading_config()
        except ConfigurationError as exc:
            raise SheetReadingUnavailable(str(exc)) from exc

        image_ids = attrs["image_ids"]
        if len(image_ids) != len(set(image_ids)):
            raise serializers.ValidationError(
                {"image_ids": "An uploaded image may only appear once in a batch."}
            )
        user = self.context["request"].user
        images = list(ImageModel.objects.filter(created_by=user, pk__in=image_ids))
        images_by_id = {image.pk: image for image in images}
        if len(images_by_id) != len(image_ids):
            raise serializers.ValidationError(
                {
                    "image_ids": "One or more images are unavailable or do not belong to you."
                }
            )
        if AnswerSheet.objects.filter(
            batch__exam=attrs["exam"], image_id__in=image_ids
        ).exists():
            raise serializers.ValidationError(
                {"image_ids": "One or more images were already uploaded for this exam."}
            )
        attrs["images"] = [images_by_id[image_id] for image_id in image_ids]
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        validated_data.pop("image_ids")
        images = validated_data.pop("images")
        batch = ScanBatch.objects.create(
            uploaded_by=request.user,
            total_sheets=len(images),
            **validated_data,
        )
        AnswerSheet.objects.bulk_create(
            [AnswerSheet(batch=batch, image=image) for image in images],
            batch_size=1000,
        )
        from app.tasks import process_batch_task

        def _enqueue():
            try:
                process_batch_task.delay(batch.pk)
            except Exception as exc:
                ScanBatch.objects.filter(
                    pk=batch.pk, status=ScanBatch.Status.PENDING
                ).delete()
                raise SheetReadingUnavailable(
                    "The background worker is unavailable. Start Redis and the Celery worker."
                ) from exc

        transaction.on_commit(_enqueue)
        return batch


class StudentAnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentAnswer
        fields = (
            "question_number",
            "selected_options",
            "needs_review",
            "source",
        )
        read_only_fields = ("needs_review", "source")


class AnswerSheetSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    answers = StudentAnswerSerializer(many=True, required=False)

    class Meta:
        model = AnswerSheet
        fields = "__all__"
        read_only_fields = (
            "batch",
            "image",
            "student",
            "scanned_mcq_marks",
            "status",
            "error_message",
            "processed_at",
            "review_reasons",
        )

    def get_image_url(self, obj):
        return obj.image.image.url if obj.image.image else None

    def validate_answers(self, value):
        if self.instance is None:
            raise serializers.ValidationError("Answers can only be updated on a sheet.")
        questions = self.instance.batch.exam.questions.order_by("pk")
        counts = {
            number: question.options_count
            for number, question in enumerate(questions, start=1)
        }
        seen = set()
        cleaned = []
        for item in value:
            number = item["question_number"]
            if number not in counts:
                raise serializers.ValidationError(f"Invalid question number: {number}.")
            if number in seen:
                raise serializers.ValidationError(f"Question {number} appears twice.")
            seen.add(number)
            letters, bad = normalize_options(item["selected_options"])
            allowed = {chr(ord("A") + index) for index in range(counts[number])}
            if bad or not set(letters).issubset(allowed):
                raise serializers.ValidationError(
                    f"Question {number} has an invalid option."
                )
            cleaned.append({"question_number": number, "selected_options": letters})
        return cleaned

    @transaction.atomic
    def update(self, instance, validated_data):
        answers = validated_data.pop("answers", None)
        sheet = super().update(instance, validated_data)
        if answers is not None:
            user = self.context["request"].user
            for item in answers:
                StudentAnswer.objects.update_or_create(
                    sheet=sheet,
                    question_number=item["question_number"],
                    defaults={
                        "selected_options": item["selected_options"],
                        "needs_review": False,
                        "source": StudentAnswer.Source.MANUAL_EDIT,
                        "updated_by": user,
                    },
                )
            from app.services.grading import recompute_sheet

            sheet = recompute_sheet(sheet.pk)
        return sheet


class AssignStudentSerializer(serializers.Serializer):
    student = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(),
        required=False,
        allow_null=True,
    )

    def validate_student(self, value):
        if value is not None and value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError(
                "The selected student does not belong to your account."
            )
        return value
