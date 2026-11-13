from rest_framework import serializers

MAX_BATCH = 40
MAX_QUESTIONS = 300


def normalize_answer_key(value: dict) -> dict[str, str]:
    """{"1": "a", 2: "C"} -> {"1": "A", "2": "C"}; rejects anything else."""
    if not isinstance(value, dict):
        raise serializers.ValidationError("Answer key must be an object.")
    normalized: dict[str, str] = {}
    for question, option in value.items():
        try:
            number = int(question)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                "Answer key keys must be question numbers."
            )
        letter = str(option or "").strip().upper()
        if not 1 <= number <= MAX_QUESTIONS:
            raise serializers.ValidationError(
                f"Question numbers must be between 1 and {MAX_QUESTIONS}."
            )
        if len(letter) != 1 or not "A" <= letter <= "Z":
            raise serializers.ValidationError(
                f"Invalid key entry for question {question!r}."
            )
        normalized[str(number)] = letter
    if not normalized:
        raise serializers.ValidationError("Answer key is empty.")
    return normalized


def normalize_file_ids(value: list[int]) -> list[int]:
    if len(value) > MAX_BATCH:
        raise serializers.ValidationError(
            f"Batch too large — send at most {MAX_BATCH} sheets at once."
        )
    # De-duplicate while keeping order.
    return list(dict.fromkeys(value))


class AssessmentCreateSerializer(serializers.Serializer):
    """POST /api/omr/assessments/ — creates the exam and starts the job."""

    title = serializers.CharField(
        max_length=200, required=False, default="Untitled Assessment"
    )
    subject = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    class_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    exam_date = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    assessment_type = serializers.ChoiceField(
        choices=["Written", "OMR", "Mixed"], required=False, default="OMR"
    )
    answer_key = serializers.DictField(help_text='{"1": "A", "2": "C", ...}')
    correct_marks = serializers.IntegerField(min_value=0, required=False, default=1)
    wrong_marks = serializers.IntegerField(required=False, default=0)
    blank_marks = serializers.IntegerField(required=False, default=0)
    file_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        help_text="FileModel ids of the uploaded sheets (batch).",
    )

    def validate_answer_key(self, value: dict) -> dict:
        return normalize_answer_key(value)

    def validate_file_ids(self, value: list[int]) -> list[int]:
        return normalize_file_ids(value)


class AssessmentUpdateSerializer(serializers.Serializer):
    """PATCH /api/omr/assessments/{id}/ — edit metadata, key or mark policy.

    Key / mark changes rescore every stored result without re-reading images.
    """

    title = serializers.CharField(max_length=200, required=False)
    subject = serializers.CharField(max_length=100, required=False, allow_blank=True)
    class_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    exam_date = serializers.CharField(max_length=64, required=False, allow_blank=True)
    assessment_type = serializers.ChoiceField(
        choices=["Written", "OMR", "Mixed"], required=False
    )
    answer_key = serializers.DictField(required=False)
    correct_marks = serializers.IntegerField(min_value=0, required=False)
    wrong_marks = serializers.IntegerField(required=False)
    blank_marks = serializers.IntegerField(required=False)

    def validate_answer_key(self, value: dict) -> dict:
        return normalize_answer_key(value)


class AddSheetsSerializer(serializers.Serializer):
    """POST /api/omr/assessments/{id}/sheets/ — grade more sheets later."""

    file_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False
    )

    def validate_file_ids(self, value: list[int]) -> list[int]:
        return normalize_file_ids(value)


class SheetVerifySerializer(serializers.Serializer):
    """PATCH /api/omr/sheets/{id}/verify/ — teacher confirms a flagged answer."""

    question_no = serializers.IntegerField(min_value=1)
    selected = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None
    )

    def validate_selected(self, value):
        if value in (None, ""):
            return None
        letter = str(value).strip().upper()
        if len(letter) != 1 or not "A" <= letter <= "Z":
            raise serializers.ValidationError("Option must be a single letter.")
        return letter


class SheetRollSerializer(serializers.Serializer):
    """PATCH /api/omr/sheets/{id}/ — teacher fixes a misread roll number."""

    roll_no = serializers.CharField(max_length=64, allow_blank=True)
