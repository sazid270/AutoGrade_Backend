from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from app.models.exams import AnswerKey, Exam, Question
from app.models.scanning import ExamScanConfiguration
from app.tasks import regrade_exam_task
from filesystem.models.models import ImageModel


def _normalize_title_component(value):
    return "".join(
        character
        for character in slugify(value, allow_unicode=True)
        if character.isalnum()
    )


def _canonical_exam_title(title, subject, exam_year):
    parts = (
        _normalize_title_component(subject),
        _normalize_title_component(title),
        _normalize_title_component(exam_year),
    )
    return "-".join(parts) if all(parts) else None


def _base_exam_title(title, subject, exam_year):
    prefix = f"{_normalize_title_component(subject)}-"
    suffix = f"-{_normalize_title_component(exam_year)}"
    if (
        prefix != "-"
        and suffix != "-"
        and title.startswith(prefix)
        and title.endswith(suffix)
    ):
        return title[len(prefix) : -len(suffix)]
    return title


class ExamSerializer(serializers.ModelSerializer):
    title = serializers.CharField(
        max_length=255,
        help_text="Normalized as subject-title-year using lowercase letters and numbers.",
    )
    exam_type = serializers.ChoiceField(
        choices=Exam.Type.choices, default=Exam.Type.MCQ
    )

    class Meta:
        model = Exam
        fields = "__all__"
        read_only_fields = (
            "created_by",
            "created_at",
            "updated_at",
        )

    @transaction.atomic
    def create(self, validated_data):
        exam = Exam.objects.create(
            created_by=self.context["request"].user, **validated_data
        )
        ExamScanConfiguration.objects.create(exam=exam)
        return exam

    def validate(self, attrs):
        marks = attrs.get("marks")
        if marks is not None and marks <= 0:
            raise serializers.ValidationError({"marks": "Must be positive."})
        if self.instance is None and not attrs.get("exam_year", "").strip():
            raise serializers.ValidationError({"exam_year": "This field is required."})
        title_changed = any(
            field in attrs for field in ("title", "subject", "exam_year")
        )
        if self.instance is not None and not title_changed:
            return attrs

        title = attrs.get("title")
        if title is None and self.instance is not None:
            title = _base_exam_title(
                self.instance.title,
                self.instance.subject,
                self.instance.exam_year,
            )
        subject = attrs.get("subject", getattr(self.instance, "subject", ""))
        exam_year = attrs.get("exam_year", getattr(self.instance, "exam_year", ""))
        canonical_title = _canonical_exam_title(title, subject, exam_year)
        if canonical_title is None:
            raise serializers.ValidationError(
                {
                    field: "Must contain at least one letter or number."
                    for field, value in (
                        ("title", title),
                        ("subject", subject),
                        ("exam_year", exam_year),
                    )
                    if not _normalize_title_component(value)
                }
            )
        if len(canonical_title) > 255:
            raise serializers.ValidationError(
                {"title": "The normalized title must be 255 characters or fewer."}
            )
        attrs["title"] = canonical_title

        owner = self.context["request"].user
        existing_exams = Exam.objects.filter(created_by=owner)
        if self.instance is not None:
            existing_exams = existing_exams.exclude(pk=self.instance.pk)
        for exam in existing_exams.only("title", "subject", "exam_year").iterator():
            base_title = _base_exam_title(
                exam.title,
                exam.subject,
                exam.exam_year,
            )
            if (
                _canonical_exam_title(base_title, exam.subject, exam.exam_year)
                == canonical_title
            ):
                raise serializers.ValidationError(
                    {"title": "You already have an exam with this title."}
                )
        return attrs

    def update(self, instance, validated_data):
        locked_fields = (
            "class_or_batch",
            "section",
            "institution",
            "exam_year",
            "exam_type",
            "marks",
        )
        if instance.batches.exists() and any(
            field in validated_data
            and validated_data[field] != getattr(instance, field)
            for field in locked_fields
        ):
            raise serializers.ValidationError(
                "Exam identity fields cannot change after scanning begins."
            )
        return super().update(instance, validated_data)


class QuestionSerializer(serializers.ModelSerializer):
    question_images = serializers.PrimaryKeyRelatedField(
        queryset=ImageModel.objects.all(),
        many=True,
        required=True,
        allow_empty=False,
        help_text="Image IDs returned by POST /api/file/image/.",
    )
    question_image_urls = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = (
            "id",
            "exam",
            "question_images",
            "question_image_urls",
            "options_count",
            "allow_multiple",
        )
        extra_kwargs = {
            "options_count": {
                "default": 4,
                "help_text": "Number of MCQ choices; defaults to 4 (A-D).",
            },
            "allow_multiple": {
                "default": False,
                "help_text": "Whether multiple choices may be selected.",
            },
        }

    def get_question_image_urls(self, obj):
        return [image.image.url for image in obj.question_images.all() if image.image]

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

    def validate_question_images(self, value):
        if any(
            image.created_by_id != self.context["request"].user.id for image in value
        ):
            raise serializers.ValidationError(
                "You can only use images that you uploaded."
            )
        return value

    def validate(self, attrs):
        question = self.instance
        if question is not None and "exam" in self.initial_data:
            raise serializers.ValidationError(
                {"exam": "The exam cannot be changed after question creation."}
            )
        if question is not None:
            if question.exam.batches.exists() and any(
                field in attrs
                for field in (
                    "question_images",
                    "options_count",
                    "allow_multiple",
                )
            ):
                if any(
                    (
                        sorted(image.pk for image in attrs[field])
                        != sorted(question.question_images.values_list("pk", flat=True))
                        if field == "question_images"
                        else attrs[field] != getattr(question, field)
                    )
                    for field in attrs
                ):
                    raise serializers.ValidationError(
                        "Question fields cannot change after scanning begins."
                    )
        if question is not None:
            key = getattr(question, "answer_key", None)
            if key:
                options_count = attrs.get("options_count", question.options_count)
                allow_multiple = attrs.get("allow_multiple", question.allow_multiple)
                options = key.correct_options
                allowed = {chr(ord("A") + index) for index in range(options_count)}
                if question.exam.exam_type != Exam.Type.MCQ or not set(
                    options
                ).issubset(allowed):
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
        return super().update(instance, validated_data)


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
        if question.exam.exam_type != Exam.Type.MCQ:
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
        options_changed = "correct_options" in validated_data and sorted(
            validated_data["correct_options"]
        ) != sorted(instance.correct_options)
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
