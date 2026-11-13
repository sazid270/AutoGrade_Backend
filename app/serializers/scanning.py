from django.db import transaction
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers

from app.models.scanning import AnswerSheet, ScanBatch, StudentAnswer
from app.services.grading import grade_answer

MAX_SCAN_IMAGE_SIZE = 20 * 1024 * 1024
MAX_SCAN_IMAGE_DIMENSION = 10000
MAX_SCAN_IMAGE_PIXELS = 40_000_000
MAX_SCAN_REQUEST_SIZE = 200 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png"}


class ScanBatchSerializer(serializers.ModelSerializer):
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

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()
        if self.instance is not None:
            extra_kwargs["exam"] = {"read_only": True}
        return extra_kwargs

    def validate_exam(self, value):
        if value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError("You cannot use another user's exam.")
        return value

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        images = self.context["request"].FILES.getlist("images")
        if not images:
            raise serializers.ValidationError(
                {"images": "Upload at least one image using the images field."}
            )
        if sum(image.size for image in images) > MAX_SCAN_REQUEST_SIZE:
            raise serializers.ValidationError(
                {"images": "The total upload size must be 200 MB or smaller."}
            )
        for image in images:
            try:
                with Image.open(image) as opened:
                    image_format = opened.format
                    width, height = opened.size
                    opened.verify()
            except (
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
                UnidentifiedImageError,
                OSError,
            ) as exc:
                raise serializers.ValidationError(
                    {"images": "Every uploaded file must be a valid image."}
                ) from exc
            if image_format not in {"JPEG", "PNG"}:
                raise serializers.ValidationError(
                    {"images": "Only JPEG and PNG images are supported."}
                )
            if max(width, height) > MAX_SCAN_IMAGE_DIMENSION:
                raise serializers.ValidationError(
                    {"images": "Image dimensions must not exceed 10000 pixels."}
                )
            if width * height > MAX_SCAN_IMAGE_PIXELS:
                raise serializers.ValidationError(
                    {"images": "Image dimensions contain too many pixels."}
                )
            if image.size > MAX_SCAN_IMAGE_SIZE:
                raise serializers.ValidationError(
                    {"images": "Each image must be 20 MB or smaller."}
                )
            image.seek(0)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        images = request.FILES.getlist("images")
        batch = ScanBatch.objects.create(
            uploaded_by=request.user,
            total_sheets=len(images),
            **validated_data,
        )
        AnswerSheet.objects.bulk_create(
            [AnswerSheet(batch=batch, image=image) for image in images]
        )
        return batch


class AnswerSheetSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnswerSheet
        fields = "__all__"
        read_only_fields = (
            "batch",
            "image",
            "student",
            "status",
            "detected_roll_no",
            "detected_name",
            "roll_confidence",
            "error_message",
            "processed_at",
        )


class StudentAnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentAnswer
        fields = "__all__"
        read_only_fields = (
            "sheet",
            "question",
            "is_correct",
            "marks_awarded",
            "fill_ratios",
            "flag",
            "confidence",
            "is_overridden",
        )

    def validate(self, attrs):
        request_user = self.context["request"].user
        sheet = attrs.get("sheet", getattr(self.instance, "sheet", None))
        question = attrs.get("question", getattr(self.instance, "question", None))
        if sheet and sheet.batch.uploaded_by_id != request_user.id:
            raise serializers.ValidationError(
                {"sheet": "You cannot use another user's sheet."}
            )
        if sheet and question and question.exam_id != sheet.batch.exam_id:
            raise serializers.ValidationError(
                {"question": "Question must belong to the sheet's exam."}
            )
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        result = instance.sheet.result.first()
        if result and result.finalized:
            raise serializers.ValidationError(
                "Answers for a finalized result cannot be edited."
            )
        selected = validated_data.get("selected_options")
        if selected is None:
            return super().update(instance, validated_data)
        if not isinstance(selected, list):
            raise serializers.ValidationError(
                {"selected_options": "Options must be provided as a list."}
            )
        normalized = sorted({str(option).strip().upper() for option in selected})
        question = instance.question
        allowed = {chr(ord("A") + index) for index in range(question.options_count)}
        if not set(normalized).issubset(allowed):
            raise serializers.ValidationError(
                {"selected_options": "Options must be valid letters for this question."}
            )
        if not question.allow_multiple and len(normalized) > 1:
            raise serializers.ValidationError(
                {"selected_options": "This question allows only one option."}
            )
        key = getattr(question, "answer_key", None)
        exam = question.exam
        is_correct, marks = grade_answer(question, normalized, key, exam)
        instance.selected_options = normalized
        instance.is_correct = is_correct
        instance.marks_awarded = marks
        instance.is_overridden = True
        instance.save(
            update_fields=(
                "selected_options",
                "is_correct",
                "marks_awarded",
                "is_overridden",
            )
        )
        result = instance.sheet.result.filter(
            finalized=False,
            is_overridden=False,
        ).first()
        if result:
            result.recalculate().save(
                update_fields=("mcq_marks", "cq_marks", "total_marks", "updated_at")
            )
        return instance
