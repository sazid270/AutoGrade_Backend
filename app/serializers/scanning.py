from django.db import transaction
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers

from app.models.scanning import (
    AnswerSheet,
    ExamScanConfiguration,
    ScanBatch,
    StudentAnswer,
)
from app.models.students import Student
from app.services.grading import grade_answer
from app.utils.student_keys import normalize_roll_no, normalize_student_key

MAX_SCAN_IMAGE_SIZE = 20 * 1024 * 1024
MAX_SCAN_IMAGE_DIMENSION = 10000
MAX_SCAN_IMAGE_PIXELS = 40_000_000
MAX_SCAN_REQUEST_SIZE = 200 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png"}


class ExamScanConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamScanConfiguration
        fields = (
            "exam",
            "omr_template",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("exam", "created_at", "updated_at")

    def validate_omr_template(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("OMR template must be an object.")
        warp_size = value.get("warp_size", (2100, 2970))
        if (
            not isinstance(warp_size, (list, tuple))
            or len(warp_size) != 2
            or not all(isinstance(item, int) and item > 0 for item in warp_size)
        ):
            raise serializers.ValidationError(
                "warp_size must contain two positive integers."
            )
        answers = value.get("answers", [])
        if not isinstance(answers, list):
            raise serializers.ValidationError("answers must be a list.")
        rois = []
        question_ids = []
        for answer in answers:
            if (
                not isinstance(answer, dict)
                or not isinstance(answer.get("bubbles"), list)
                or not isinstance(answer.get("question_id"), int)
                or isinstance(answer.get("question_id"), bool)
                or answer["question_id"] <= 0
            ):
                raise serializers.ValidationError(
                    "Each answer needs a question_id and bubble ROI list."
                )
            question_ids.append(answer["question_id"])
            rois.extend(answer["bubbles"])
        if len(question_ids) != len(set(question_ids)):
            raise serializers.ValidationError(
                "Each question_id can only appear once in the OMR template."
            )
        roll = value.get("roll_number", {})
        if not isinstance(roll, dict) or not isinstance(roll.get("columns", []), list):
            raise serializers.ValidationError("roll_number.columns must be a list.")
        for column in roll.get("columns", []):
            if not isinstance(column, dict) or not isinstance(
                column.get("bubbles"), list
            ):
                raise serializers.ValidationError("Each roll column needs bubble ROIs.")
            rois.extend(column["bubbles"])
        if len(roll.get("columns", [])) > 32:
            raise serializers.ValidationError(
                "roll_number cannot contain more than 32 columns."
            )
        student_fields = value.get("student_fields", {})
        if not isinstance(student_fields, dict):
            raise serializers.ValidationError("student_fields must be an object.")
        allowed_student_fields = {
            "name",
            "roll_no",
            "registration_no",
            "subject_code",
            "set_code",
        }
        max_lengths = {
            "name": 255,
            "roll_no": 32,
            "registration_no": 64,
            "subject_code": 32,
            "set_code": 32,
        }
        for field_name, field_config in student_fields.items():
            if field_name not in allowed_student_fields:
                raise serializers.ValidationError(
                    f"Unsupported student field: {field_name}."
                )
            if (
                not isinstance(field_config, dict)
                or not isinstance(field_config.get("columns"), list)
                or not field_config["columns"]
            ):
                raise serializers.ValidationError(
                    f"student_fields.{field_name}.columns must be a non-empty list."
                )
            if len(field_config["columns"]) > max_lengths[field_name]:
                raise serializers.ValidationError(
                    f"student_fields.{field_name} exceeds its maximum field length."
                )
            for column in field_config["columns"]:
                if not isinstance(column, dict) or not isinstance(
                    column.get("bubbles"), list
                ):
                    raise serializers.ValidationError(
                        f"Each {field_name} column needs bubble ROIs."
                    )
                symbols = column.get("symbols", "0123456789")
                if (
                    not isinstance(symbols, str)
                    or len(symbols) != len(column["bubbles"])
                    or len(symbols) > 26
                    or len(set(symbols)) != len(symbols)
                    or not symbols
                ):
                    raise serializers.ValidationError(
                        f"Each {field_name} column needs one symbol per bubble."
                    )
                rois.extend(column["bubbles"])
        width, height = warp_size
        for roi in rois:
            if (
                not isinstance(roi, list)
                or len(roi) != 4
                or not all(isinstance(item, int) for item in roi)
                or roi[0] < 0
                or roi[1] < 0
                or roi[2] <= 0
                or roi[3] <= 0
                or roi[0] + roi[2] > width
                or roi[1] + roi[3] > height
            ):
                raise serializers.ValidationError(
                    "Every bubble ROI must lie within warp_size."
                )
        markers = value.get("corner_markers")
        if markers is not None and (
            not isinstance(markers, list)
            or len(markers) != 4
            or any(
                not isinstance(point, list)
                or len(point) != 2
                or point[0] < 0
                or point[1] < 0
                or point[0] >= width
                or point[1] >= height
                for point in markers
            )
        ):
            raise serializers.ValidationError(
                "corner_markers must contain four destination points within warp_size."
            )
        return value

    def update(self, instance, validated_data):
        template = validated_data.get("omr_template", instance.omr_template)
        if instance.exam.batches.exists() and template != instance.omr_template:
            raise serializers.ValidationError(
                "The OMR template cannot change after sheets have been uploaded."
            )
        return super().update(instance, validated_data)


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
        exam = attrs.get("exam")
        if exam:
            from app.services.grading import validate_exam_template

            try:
                validate_exam_template(exam)
            except ValueError as exc:
                raise serializers.ValidationError({"exam": str(exc)}) from exc
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
            "roll_confidence",
            "error_message",
            "processed_at",
        )

    @transaction.atomic
    def update(self, instance, validated_data):
        sheet = super().update(instance, validated_data)
        student = sheet.student
        if student is None:
            return sheet

        updated_fields = []
        if "detected_roll_no" in validated_data:
            roll_no = normalize_roll_no(sheet.detected_roll_no)
            duplicate = Student.objects.filter(
                created_by=student.created_by,
                institution=student.institution,
                grade=student.grade,
                section=student.section,
                academic_year=student.academic_year,
                roll_no=roll_no,
            ).exclude(pk=student.pk)
            if duplicate.exists():
                raise serializers.ValidationError(
                    {"detected_roll_no": "This roll number is already in use."}
                )
            student.roll_no = roll_no
            updated_fields.append("roll_no")
        for sheet_field, student_field in (
            ("detected_registration_no", "registration_no"),
            ("detected_name", "name"),
        ):
            if sheet_field in validated_data:
                value = getattr(sheet, sheet_field)
                if sheet_field == "detected_registration_no":
                    value = normalize_student_key(value)
                    duplicate = Student.objects.filter(
                        created_by=student.created_by,
                        institution=student.institution,
                        registration_no=value,
                    ).exclude(pk=student.pk)
                    if value and duplicate.exists():
                        raise serializers.ValidationError(
                            {sheet_field: "This registration number is already in use."}
                        )
                    sheet.detected_registration_no = value
                    sheet.save(update_fields=("detected_registration_no",))
                setattr(student, student_field, value)
                updated_fields.append(student_field)
        if updated_fields:
            student.save(update_fields=updated_fields)
        return sheet


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
        is_correct = grade_answer(normalized, key)
        instance.selected_options = normalized
        instance.is_correct = is_correct
        instance.marks_awarded = 0
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
