from decimal import Decimal

from rest_framework import serializers

from app.models.results import Result


# Result serializers
class ResultSerializer(serializers.ModelSerializer):
    total_marks = serializers.DecimalField(
        max_digits=7, decimal_places=2, read_only=True
    )

    class Meta:
        model = Result
        fields = "__all__"
        read_only_fields = (
            "exam",
            "student",
            "sheet",
            "is_overridden",
            "updated_by",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs):
        request_user = self.context["request"].user
        exam = attrs.get("exam", getattr(self.instance, "exam", None))
        student = attrs.get("student", getattr(self.instance, "student", None))
        sheet = attrs.get("sheet", getattr(self.instance, "sheet", None))
        if exam and exam.created_by_id != request_user.id:
            raise serializers.ValidationError(
                {"exam": "You cannot use another user's exam."}
            )
        if student and student.created_by_id != request_user.id:
            raise serializers.ValidationError(
                {"student": "You cannot use another user's student."}
            )
        duplicate = Result.objects.filter(exam=exam, student=student)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if exam and student and duplicate.exists():
            raise serializers.ValidationError(
                {"student": "A result already exists for this student and exam."}
            )
        if sheet and (
            sheet.batch.uploaded_by_id != request_user.id
            or sheet.batch.exam_id != getattr(exam, "id", None)
            or (student and sheet.student_id not in (None, student.id))
        ):
            raise serializers.ValidationError(
                {
                    "sheet": "Sheet, exam, and student must belong to the same account and exam."
                }
            )
        if {"mcq_marks", "cq_marks"} & set(attrs):
            mcq_marks = attrs.get(
                "mcq_marks", getattr(self.instance, "mcq_marks", Decimal("0"))
            )
            cq_marks = attrs.get(
                "cq_marks", getattr(self.instance, "cq_marks", Decimal("0"))
            ) or Decimal("0")
            if mcq_marks < 0 or cq_marks < 0:
                raise serializers.ValidationError("Marks cannot be negative.")
            if exam.exam_type == exam.Type.MCQ and cq_marks:
                raise serializers.ValidationError(
                    {"cq_marks": "CQ marks are not available for an MCQ exam."}
                )
            if mcq_marks + cq_marks > exam.marks:
                raise serializers.ValidationError(
                    "Marks cannot exceed the exam's total marks."
                )
        return attrs

    def update(self, instance, validated_data):
        if set(validated_data) - {"finalized"}:
            validated_data["is_overridden"] = True
        validated_data["updated_by"] = self.context["request"].user
        return super().update(instance, validated_data)
