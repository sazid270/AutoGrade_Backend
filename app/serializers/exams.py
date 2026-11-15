from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from app.models.exams import AnswerKey, Exam, Question
from app.models.scanning import ExamScanConfiguration
from app.services.answer_keys import ensure_exam_questions
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

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.exam_type != Exam.Type.MCQ:
            data.pop("negative_marks", None)
        return data

    @transaction.atomic
    def create(self, validated_data):
        exam = Exam.objects.create(
            created_by=self.context["request"].user, **validated_data
        )
        ExamScanConfiguration.objects.create(exam=exam)
        return exam

    def validate(self, attrs):
        exam_type = attrs.get(
            "exam_type",
            self.instance.exam_type
            if self.instance is not None
            else Exam.Type.MCQ,
        )
        if exam_type != Exam.Type.MCQ:
            if "negative_marks" in attrs:
                raise serializers.ValidationError(
                    {"negative_marks": "This field is only valid for MCQ exams."}
                )
            if (
                self.instance is not None
                and "exam_type" in attrs
                and self.instance.negative_marks != 0
            ):
                raise serializers.ValidationError(
                    {
                        "negative_marks": (
                            "Set this to 0 before changing the exam type to CQ."
                        )
                    }
                )
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

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = Exam.objects.select_for_update().get(pk=instance.pk)
        if (
            instance.questions.exists()
            and "exam_type" in validated_data
            and validated_data["exam_type"] != instance.exam_type
        ):
            raise serializers.ValidationError(
                "Exam type cannot change after questions have been added."
            )
        locked_fields = (
            "class_or_batch",
            "section",
            "institution",
            "exam_year",
            "exam_type",
            "marks",
            "negative_marks",
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

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.exam.exam_type != Exam.Type.MCQ:
            data.pop("options_count", None)
            data.pop("allow_multiple", None)
        return data

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
        exam = attrs.get("exam", question.exam if question is not None else None)
        if exam is not None and exam.exam_type != Exam.Type.MCQ:
            mcq_fields = ("options_count", "allow_multiple")
            supplied_fields = [
                field for field in mcq_fields if field in self.initial_data
            ]
            if supplied_fields:
                raise serializers.ValidationError(
                    {
                        field: "This field is only valid for MCQ exams."
                        for field in supplied_fields
                    }
                )
            for field in mcq_fields:
                attrs.pop(field, None)
        if question is not None:
            image_changed = (
                "question_images" in attrs
                and sorted(image.pk for image in attrs["question_images"])
                != sorted(question.question_images.values_list("pk", flat=True))
            )
            config_changed = any(
                attrs[field] != getattr(question, field)
                for field in ("options_count", "allow_multiple")
                if field in attrs
            )
            if question.exam.batches.exists() and (image_changed or config_changed):
                raise serializers.ValidationError(
                    "Question fields cannot change after scanning begins."
                )
            if image_changed and getattr(question, "answer_key", None):
                raise serializers.ValidationError(
                    "Question images cannot change after answers are entered."
                )
        if question is not None:
            key = getattr(question, "answer_key", None)
            if key and question.exam.exam_type == Exam.Type.MCQ:
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

    @transaction.atomic
    def create(self, validated_data):
        exam = Exam.objects.select_for_update().get(pk=validated_data["exam"].pk)
        validated_data["exam"] = exam
        if exam.batches.exists():
            raise serializers.ValidationError(
                "Questions cannot be added after scanning begins."
            )
        if AnswerKey.objects.filter(exam=exam).exists():
            raise serializers.ValidationError(
                "Questions cannot be added after answers have been entered."
            )
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        exam = Exam.objects.select_for_update().get(pk=instance.exam_id)
        instance = Question.objects.select_for_update().get(pk=instance.pk, exam=exam)
        new_images = validated_data.get("question_images")
        images_changed = new_images is not None and {
            image.pk for image in new_images
        } != set(instance.question_images.values_list("pk", flat=True))
        if images_changed and (
            exam.batches.exists()
            or AnswerKey.objects.filter(question=instance).exists()
        ):
            raise serializers.ValidationError(
                "Question images cannot change after answers are entered "
                "or scanning begins."
            )
        return super().update(instance, validated_data)


class AnswerEntrySerializer(serializers.Serializer):
    question_number = serializers.IntegerField(
        min_value=1,
        help_text="The question number printed in the answer key.",
    )
    correct_options = serializers.ListField(
        child=serializers.CharField(min_length=1, max_length=1),
        allow_empty=False,
        help_text="Correct option letters, such as ['B'] or ['A', 'C'].",
    )


class ExamAnswerSetSerializer(serializers.Serializer):
    source = serializers.ChoiceField(
        choices=("manual", "image"),
        default="manual",
        help_text="Use 'image' after reviewing an extracted image draft.",
    )
    answers = AnswerEntrySerializer(many=True, allow_empty=False)

    def validate(self, attrs):
        exam = self.context["exam"]
        if exam.exam_type != Exam.Type.MCQ:
            raise serializers.ValidationError(
                "Teacher answers are currently supported for MCQ exams only."
            )

        received_numbers = [item["question_number"] for item in attrs["answers"]]
        if len(received_numbers) != len(set(received_numbers)):
            raise serializers.ValidationError(
                {"answers": "Each question_number can appear only once."}
            )

        questions = list(exam.questions.order_by("pk"))
        normalized_answers = []
        for item in attrs["answers"]:
            question_number = item["question_number"]
            question = (
                questions[question_number - 1]
                if question_number <= len(questions)
                else None
            )
            options = sorted(
                {option.strip().upper() for option in item["correct_options"]}
            )
            if not set(options).issubset(set("ABCDEFGHIJ")):
                raise serializers.ValidationError(
                    {
                        "answers": (
                            f"Question {question_number} has an option outside A through J."
                        )
                    }
                )
            if question is not None and not set(options).issubset(
                {
                    chr(ord("A") + index)
                    for index in range(question.options_count)
                }
            ):
                raise serializers.ValidationError(
                    {
                        "answers": (
                            f"Question {question_number} has an option outside its "
                            "configured options_count."
                        )
                    }
                )
            if question is not None and not question.allow_multiple and len(options) > 1:
                raise serializers.ValidationError(
                    {
                        "answers": (
                            f"Question {question_number} allows only one correct option."
                        )
                    }
                )
            normalized_answers.append((question_number, options))

        attrs["normalized_answers"] = normalized_answers
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        exam = Exam.objects.select_for_update().get(pk=self.context["exam"].pk)
        source = validated_data["source"]
        model_source = (
            AnswerKey.Source.SCANNED if source == "image" else AnswerKey.Source.MANUAL
        )
        questions = ensure_exam_questions(
            exam,
            dict(validated_data["normalized_answers"]),
        )
        question_ids_to_regrade = set()
        entries = []
        for question_number, options in validated_data["normalized_answers"]:
            question = questions[question_number - 1]
            key, created = AnswerKey.objects.select_for_update().get_or_create(
                exam=exam,
                question_number=question_number,
                defaults={
                    "question": question,
                    "correct_options": options,
                    "source": model_source,
                    "needs_review": False,
                    "updated_by": self.context["request"].user,
                },
            )
            if created:
                question_ids_to_regrade.add(question.pk)
            else:
                previous_question_id = key.question_id
                options_changed = sorted(key.correct_options) != options
                changed = (
                    options_changed
                    or key.needs_review
                    or key.source != model_source
                    or key.question_id != question.pk
                )
                if changed:
                    key.question = question
                    key.correct_options = options
                    key.source = (
                        AnswerKey.Source.MANUAL_EDIT
                        if source == "manual"
                        else model_source
                    )
                    key.needs_review = False
                    key.updated_by = self.context["request"].user
                    key.save(
                        update_fields=(
                            "question",
                            "correct_options",
                            "source",
                            "needs_review",
                            "updated_by",
                            "updated_at",
                        )
                    )
                    if options_changed or previous_question_id != question.pk:
                        question_ids_to_regrade.add(previous_question_id)
                        question_ids_to_regrade.add(question.pk)
            entries.append(key)

        if question_ids_to_regrade:
            question_ids = sorted(question_ids_to_regrade)
            transaction.on_commit(
                lambda exam_id=exam.pk, ids=question_ids: regrade_exam_task.delay(
                    exam_id, ids
                )
            )
        return entries


class AnswerImageExtractionRequestSerializer(serializers.Serializer):
    image_id = serializers.PrimaryKeyRelatedField(
        queryset=ImageModel.objects.all(),
        help_text=(
            "Image ID returned. The image is sent to the "
            "configured extraction service for processing."
        ),
    )

    def validate_image_id(self, value):
        if value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError(
                "You can only process an image that you uploaded."
            )
        return value


class SavedAnswerEntrySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    exam = serializers.IntegerField(source="exam_id")
    question = serializers.IntegerField(source="question_id")
    question_number = serializers.IntegerField()
    correct_options = serializers.ListField(child=serializers.CharField())
    source = serializers.ChoiceField(choices=AnswerKey.Source.choices, allow_null=True)
    needs_review = serializers.BooleanField()


class AnswerConflictSerializer(serializers.Serializer):
    question_number = serializers.IntegerField()
    existing_options = serializers.ListField(child=serializers.CharField())
    extracted_options = serializers.ListField(child=serializers.CharField())


class AnswerKeyPatchSerializer(serializers.Serializer):
    correct_options = serializers.ListField(
        child=serializers.CharField(min_length=1, max_length=1),
        allow_empty=False,
    )

    def validate_correct_options(self, value):
        options = sorted({option.strip().upper() for option in value})
        if not set(options).issubset(set("ABCDEFGHIJ")):
            raise serializers.ValidationError("Options must be letters A through J.")

        answer_key = self.instance
        if answer_key.question_id:
            question = answer_key.question
            allowed_options = {
                chr(ord("A") + index) for index in range(question.options_count)
            }
            if not set(options).issubset(allowed_options):
                raise serializers.ValidationError(
                    "An option is outside this question's configured options_count."
                )
            if not question.allow_multiple and len(options) > 1:
                raise serializers.ValidationError(
                    "This question allows only one correct option."
                )
        return options

    @transaction.atomic
    def update(self, instance, validated_data):
        instance.correct_options = validated_data["correct_options"]
        instance.source = AnswerKey.Source.MANUAL_EDIT
        instance.needs_review = False
        instance.updated_by = self.context["request"].user
        instance.save(
            update_fields=(
                "correct_options",
                "source",
                "needs_review",
                "updated_by",
                "updated_at",
            )
        )
        exam_id = instance.exam_id
        question_ids = [instance.question_id]
        transaction.on_commit(
            lambda: regrade_exam_task.delay(exam_id, question_ids)
        )
        return instance


class ExamAnswersResponseSerializer(serializers.Serializer):
    exam = serializers.IntegerField()
    answers = SavedAnswerEntrySerializer(many=True)


class AnswerExtractionResponseSerializer(serializers.Serializer):
    exam = serializers.IntegerField()
    source = serializers.ChoiceField(choices=("image",))
    requires_review = serializers.BooleanField()
    answers = SavedAnswerEntrySerializer(many=True)
    conflicts = AnswerConflictSerializer(many=True)
    invalid_answers = serializers.ListField(child=serializers.JSONField())
