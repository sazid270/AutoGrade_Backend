from django.db import transaction
from rest_framework import serializers

from app.models.exams import AnswerKey, Exam, Question
from app.tasks import regrade_exam_task


class ExamSerializer(serializers.ModelSerializer):
    class Meta:
        model = Exam
        fields = "__all__"
        read_only_fields = ("created_by", "created_at", "updated_at")

    def create(self, validated_data):
        return Exam.objects.create(
            created_by=self.context["request"].user, **validated_data
        )

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
        for answer in answers:
            if not isinstance(answer, dict) or not isinstance(
                answer.get("bubbles"), list
            ):
                raise serializers.ValidationError(
                    "Each answer needs a bubble ROI list."
                )
            rois.extend(answer["bubbles"])
        roll = value.get("roll_number", {})
        if not isinstance(roll, dict) or not isinstance(roll.get("columns", []), list):
            raise serializers.ValidationError("roll_number.columns must be a list.")
        for column in roll.get("columns", []):
            if not isinstance(column, dict) or not isinstance(
                column.get("bubbles"), list
            ):
                raise serializers.ValidationError("Each roll column needs bubble ROIs.")
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

    def validate(self, attrs):
        total_marks = attrs.get("total_marks")
        if total_marks is not None and total_marks <= 0:
            raise serializers.ValidationError({"total_marks": "Must be positive."})
        if self.instance is None and not attrs.get("section", ""):
            raise serializers.ValidationError(
                {"section": "A section is required for student matching."}
            )
        return attrs

    def update(self, instance, validated_data):
        locked_fields = (
            "grade",
            "section",
            "institution",
            "academic_year",
            "omr_template",
        )
        if instance.batches.exclude(status="pending").exists() and any(
            field in validated_data
            and validated_data[field] != getattr(instance, field)
            for field in locked_fields
        ):
            raise serializers.ValidationError(
                "Exam identity fields cannot change after scanning begins."
            )
        should_regrade = "negative_marking" in validated_data
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if should_regrade:
                transaction.on_commit(lambda: regrade_exam_task.delay(instance.pk))
        return instance


class QuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Question
        fields = "__all__"

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()
        if self.instance is not None:
            extra_kwargs["exam"] = {"read_only": True}
        return extra_kwargs

    def validate_options_count(self, value):
        if not 2 <= value <= 10:
            raise serializers.ValidationError("options_count must be between 2 and 10.")
        return value

    def validate_exam(self, value):
        if value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError("You cannot use another user's exam.")
        return value

    def validate(self, attrs):
        question = self.instance
        exam = attrs.get("exam", getattr(question, "exam", None))
        number = attrs.get("number", getattr(question, "number", None))
        duplicate = Question.objects.filter(exam=exam, number=number)
        if question is not None:
            if question.exam.batches.exists() and any(
                field in attrs for field in ("number", "type", "options_count", "allow_multiple")
            ):
                if any(
                    attrs.get(field, getattr(question, field)) != getattr(question, field)
                    for field in ("number", "type", "options_count", "allow_multiple")
                    if field in attrs
                ):
                    raise serializers.ValidationError(
                        "Question identity and OMR settings cannot change after scanning begins."
                    )
            duplicate = duplicate.exclude(pk=question.pk)
        if duplicate.exists():
            raise serializers.ValidationError(
                {"number": "A question with this number already exists for the exam."}
            )
        if question is not None:
            key = getattr(question, "answer_key", None)
            if key:
                options_count = attrs.get("options_count", question.options_count)
                question_type = attrs.get("type", question.type)
                allow_multiple = attrs.get("allow_multiple", question.allow_multiple)
                options = key.correct_options
                allowed = {chr(ord("A") + index) for index in range(options_count)}
                if question_type != Question.Type.MCQ or not set(options).issubset(
                    allowed
                ):
                    raise serializers.ValidationError(
                        "Question changes would invalidate its answer key."
                    )
                if not allow_multiple and len(options) > 1:
                    raise serializers.ValidationError(
                        "Question changes would invalidate its answer key."
                    )
        return attrs

    def create(self, validated_data):
        exam = validated_data["exam"]
        if exam.batches.exists():
            raise serializers.ValidationError(
                "Questions cannot be added after scanning begins."
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        should_regrade = "marks" in validated_data
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if should_regrade:
                transaction.on_commit(lambda: regrade_exam_task.delay(instance.exam_id))
        return instance


class AnswerKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = AnswerKey
        fields = "__all__"
        read_only_fields = (
            "source",
            "confidence",
            "needs_review",
            "updated_by",
            "updated_at",
        )

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()
        if self.instance is not None:
            extra_kwargs["question"] = {"read_only": True}
        return extra_kwargs

    def validate(self, attrs):
        question = attrs.get("question", getattr(self.instance, "question", None))
        options = attrs.get(
            "correct_options",
            getattr(self.instance, "correct_options", []),
        )
        if question is None:
            raise serializers.ValidationError({"question": "This field is required."})
        if question.exam.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError(
                {"question": "You cannot use another user's question."}
            )
        if question.type != Question.Type.MCQ:
            raise serializers.ValidationError(
                {"question": "CQ questions cannot have an OMR answer key."}
            )
        if not options:
            raise serializers.ValidationError(
                {"correct_options": "At least one option is required."}
            )
        if not isinstance(options, list):
            raise serializers.ValidationError(
                {"correct_options": "Options must be a list."}
            )
        if not all(isinstance(option, str) for option in options):
            raise serializers.ValidationError(
                {"correct_options": "Every option must be a string."}
            )
        options = sorted({option.strip().upper() for option in options})
        allowed = {chr(ord("A") + index) for index in range(question.options_count)}
        if not all(option in allowed for option in options):
            raise serializers.ValidationError(
                {"correct_options": "Options must be letters within options_count."}
            )
        if not question.allow_multiple and len(options) > 1:
            raise serializers.ValidationError(
                {"correct_options": "This question allows only one option."}
            )
        attrs["correct_options"] = options
        return attrs

    def create(self, validated_data):
        validated_data["source"] = AnswerKey.Source.MANUAL
        validated_data["updated_by"] = self.context["request"].user
        with transaction.atomic():
            instance = super().create(validated_data)
            transaction.on_commit(
                lambda: regrade_exam_task.delay(instance.question.exam_id)
            )
        return instance

    def update(self, instance, validated_data):
        options_changed = (
            "correct_options" in validated_data
            and sorted(validated_data["correct_options"])
            != sorted(instance.correct_options)
        )
        if not instance.needs_review and not options_changed:
            return instance
        validated_data["source"] = AnswerKey.Source.MANUAL_EDIT
        validated_data["needs_review"] = False
        validated_data["updated_by"] = self.context["request"].user
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            transaction.on_commit(
                lambda: regrade_exam_task.delay(instance.question.exam_id)
            )
        return instance
