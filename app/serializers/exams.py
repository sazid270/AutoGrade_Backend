from django.db import transaction
from rest_framework import serializers

from app.models.exams import Answer, Exam, Question
from app.services.answers import ensure_exam_questions
from app.utils.options import normalize_options
from filesystem.models.models import ImageModel


# Exam serializers
class ExamSerializer(serializers.ModelSerializer):
    exam_type = serializers.ChoiceField(
        choices=Exam.Type.choices,
        default=Exam.Type.MCQ,
    )

    class Meta:
        model = Exam
        fields = "__all__"
        read_only_fields = ("created_by", "created_at", "updated_at")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.exam_type != Exam.Type.MCQ:
            data.pop("negative_marks", None)
        return data

    @transaction.atomic
    def create(self, validated_data):
        return Exam.objects.create(
            created_by=self.context["request"].user,
            **validated_data,
        )

    def validate(self, attrs):
        exam_type = attrs.get(
            "exam_type",
            self.instance.exam_type if self.instance is not None else Exam.Type.MCQ,
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
        owner = self.context["request"].user
        existing = Exam.objects.filter(created_by=owner)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        title = attrs.get("title", getattr(self.instance, "title", None))
        if title and existing.filter(title__iexact=title).exists():
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
            "mcq_mark",
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


class AnswerEntrySerializer(serializers.Serializer):
    question_number = serializers.IntegerField(
        min_value=1,
        help_text="The question number printed in the answer key.",
    )
    correct_options = serializers.ListField(
        child=serializers.CharField(min_length=1, max_length=1),
        allow_empty=False,
        help_text="Option letters ('A'-'J') or Bangla labels ('ক'-'ঞ'), e.g. ['B'].",
    )


# Question serializers
class QuestionSerializer(serializers.ModelSerializer):
    question_images = serializers.PrimaryKeyRelatedField(
        queryset=ImageModel.objects.all(),
        many=True,
        required=False,
        allow_empty=True,
        help_text="Image IDs returned by POST /api/file/image/.",
    )
    question_image_urls = serializers.SerializerMethodField()
    options_count = serializers.IntegerField(
        min_value=2,
        max_value=10,
        required=False,
        default=4,
        help_text="Number of MCQ choices; defaults to 4 (A-D).",
    )
    allow_multiple = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Whether multiple choices may be selected.",
    )

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
        request = self.context.get("request")
        if self.instance is not None or getattr(request, "method", None) == "PATCH":
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
            image_changed = "question_images" in attrs and sorted(
                image.pk for image in attrs["question_images"]
            ) != sorted(question.question_images.values_list("pk", flat=True))
            config_changed = any(
                attrs[field] != getattr(question, field)
                for field in ("options_count", "allow_multiple")
                if field in attrs
            )
            if question.exam.batches.exists() and (image_changed or config_changed):
                raise serializers.ValidationError(
                    "Question fields cannot change after scanning begins."
                )
            if image_changed and getattr(question, "answer", None):
                raise serializers.ValidationError(
                    "Question images cannot change after answers are entered."
                )
        if question is not None:
            key = getattr(question, "answer", None)
            if key and question.exam.exam_type == Exam.Type.MCQ:
                options_count = attrs.get("options_count", question.options_count)
                allow_multiple = attrs.get("allow_multiple", question.allow_multiple)
                options = key.correct_options
                allowed = {chr(ord("A") + index) for index in range(options_count)}
                if not set(options).issubset(allowed):
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
        if exam.batches.exists() or Answer.objects.filter(exam=exam).exists():
            raise serializers.ValidationError(
                "Questions cannot be added after scanning or answers have started."
            )
        validated_data["exam"] = exam
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
            exam.batches.exists() or Answer.objects.filter(question=instance).exists()
        ):
            raise serializers.ValidationError(
                "Question images cannot change after answers are entered "
                "or scanning begins."
            )
        return super().update(instance, validated_data)


class QuestionPatchSerializer(QuestionSerializer):
    class Meta(QuestionSerializer.Meta):
        fields = (
            "id",
            "question_images",
            "question_image_urls",
            "options_count",
            "allow_multiple",
        )


# Answer serializers
class ExamAnswerSetSerializer(serializers.Serializer):
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
            options, bad = normalize_options(item["correct_options"])
            if bad or not options:
                raise serializers.ValidationError(
                    {
                        "answers": (
                            f"Question {question_number} has an unrecognised option."
                        )
                    }
                )
            if question is not None and not set(options).issubset(
                {chr(ord("A") + index) for index in range(question.options_count)}
            ):
                raise serializers.ValidationError(
                    {
                        "answers": (
                            f"Question {question_number} has an option outside its "
                            "configured options_count."
                        )
                    }
                )
            if (
                question is not None
                and not question.allow_multiple
                and len(options) > 1
            ):
                raise serializers.ValidationError(
                    {
                        "answers": (
                            f"Question {question_number} allows only one correct "
                            "option."
                        )
                    }
                )
            normalized_answers.append((question_number, options))

        attrs["normalized_answers"] = normalized_answers
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        exam = Exam.objects.select_for_update().get(pk=self.context["exam"].pk)
        user = self.context["request"].user
        question_count = exam.questions.count()
        if exam.batches.exists() and any(
            question_number > question_count
            for question_number, _ in validated_data["normalized_answers"]
        ):
            raise serializers.ValidationError(
                "Questions cannot be added after scanning begins."
            )
        questions = ensure_exam_questions(
            exam,
            dict(validated_data["normalized_answers"]),
        )
        entries = []
        any_changed = False
        for question_number, options in validated_data["normalized_answers"]:
            question = questions[question_number - 1]
            answer, created = Answer.objects.select_for_update().get_or_create(
                exam=exam,
                question_number=question_number,
                defaults={
                    "question": question,
                    "correct_options": options,
                    "source": Answer.Source.MANUAL,
                    "needs_review": False,
                    "updated_by": user,
                },
            )
            any_changed = any_changed or created
            if not created:
                changed = (
                    sorted(answer.correct_options) != options
                    or answer.needs_review
                    or answer.question_id != question.pk
                )
                if changed:
                    any_changed = True
                    answer.question = question
                    answer.correct_options = options
                    answer.source = Answer.Source.MANUAL_EDIT
                    answer.needs_review = False
                    answer.updated_by = user
                    answer.save(
                        update_fields=(
                            "question",
                            "correct_options",
                            "source",
                            "needs_review",
                            "updated_by",
                            "updated_at",
                        )
                    )
            entries.append(answer)

        if any_changed and exam.batches.exists():
            from app.services.grading import regrade_exam

            regrade_exam(exam)
        return entries


class AnswerImageExtractionRequestSerializer(serializers.Serializer):
    image_id = serializers.PrimaryKeyRelatedField(
        queryset=ImageModel.objects.all(),
    )

    def validate_image_id(self, value):
        if value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError(
                "You can only process an image that you uploaded."
            )
        return value


class SavedAnswerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    exam = serializers.IntegerField(source="exam_id")
    question = serializers.IntegerField(source="question_id")
    question_number = serializers.IntegerField()
    correct_options = serializers.ListField(child=serializers.CharField())
    source = serializers.ChoiceField(choices=Answer.Source.choices, allow_null=True)
    needs_review = serializers.BooleanField()


class AnswerConflictSerializer(serializers.Serializer):
    question_number = serializers.IntegerField()
    existing_options = serializers.ListField(child=serializers.CharField())
    extracted_options = serializers.ListField(child=serializers.CharField())


class ExamAnswersResponseSerializer(serializers.Serializer):
    exam = serializers.IntegerField()
    answers = SavedAnswerSerializer(many=True)


class AnswerExtractionResponseSerializer(serializers.Serializer):
    exam = serializers.IntegerField()
    source = serializers.ChoiceField(choices=("image",))
    requires_review = serializers.BooleanField()
    answers = SavedAnswerSerializer(many=True)
    conflicts = AnswerConflictSerializer(many=True)
    invalid_answers = serializers.ListField(child=serializers.JSONField())
