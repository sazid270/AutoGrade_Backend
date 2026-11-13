from decimal import Decimal

from rest_framework import serializers

from app.models.results import Result


class ResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = Result
        fields = "__all__"
        read_only_fields = (
            "exam",
            "student",
            "sheet",
            "total_marks",
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
        if self.instance and self.instance.finalized and attrs != {"finalized": False}:
            raise serializers.ValidationError("Finalized results cannot be edited.")
        if (
            attrs.get("finalized")
            and self.instance
            and self.instance.sheet_id
            and self.instance.sheet.status == "needs_review"
        ):
            raise serializers.ValidationError(
                {"finalized": "Resolve the answer sheet before finalizing the result."}
            )
        if {"mcq_marks", "cq_marks"} & set(attrs):
            mcq_marks = attrs.get(
                "mcq_marks", getattr(self.instance, "mcq_marks", Decimal("0"))
            )
            cq_marks = attrs.get(
                "cq_marks", getattr(self.instance, "cq_marks", Decimal("0"))
            ) or Decimal("0")
            question_total = sum(question.marks for question in exam.questions.all())
            if mcq_marks + cq_marks > question_total:
                raise serializers.ValidationError("Marks cannot exceed question marks.")
        return attrs

    def update(self, instance, validated_data):
        if set(validated_data) - {"finalized"}:
            validated_data["is_overridden"] = True
        if "mcq_marks" in validated_data or "cq_marks" in validated_data:
            validated_data["total_marks"] = validated_data.get(
                "mcq_marks", instance.mcq_marks
            ) + (validated_data.get("cq_marks", instance.cq_marks) or Decimal("0"))
        validated_data["updated_by"] = self.context["request"].user
        return super().update(instance, validated_data)
