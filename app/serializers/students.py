from rest_framework import serializers

from app.models.students import Student
from app.utils.student_keys import (
    normalize_class_name,
    normalize_roll_no,
    normalize_section,
    normalize_student_key,
)


def normalize_field(field, value):
    if field == "roll_no":
        return normalize_roll_no(value)
    if field == "section":
        return normalize_section(value)
    if field == "class_name":
        return normalize_class_name(value)
    return normalize_student_key(value)


## Student serializers
class StudentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Student
        fields = "__all__"
        read_only_fields = ("created_by", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        for field in ("institution", "class_name", "section", "academic_year"):
            validated_data[field] = normalize_field(field, validated_data.get(field))
        validated_data["roll_no"] = normalize_roll_no(validated_data.get("roll_no"))
        if "registration_no" in validated_data:
            validated_data["registration_no"] = normalize_student_key(
                validated_data["registration_no"]
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        for field in ("institution", "class_name", "section", "academic_year"):
            if field in validated_data:
                validated_data[field] = normalize_field(field, validated_data[field])
        if "roll_no" in validated_data:
            validated_data["roll_no"] = normalize_roll_no(validated_data["roll_no"])
        if "registration_no" in validated_data:
            validated_data["registration_no"] = normalize_student_key(
                validated_data["registration_no"]
            )
        return super().update(instance, validated_data)

    def validate(self, attrs):
        values = {
            field: normalize_field(
                field,
                attrs.get(field, getattr(self.instance, field, "")),
            )
            for field in (
                "institution",
                "class_name",
                "section",
                "academic_year",
                "roll_no",
            )
        }
        class_name = normalize_class_name(
            attrs.get("class_name", getattr(self.instance, "class_name", ""))
        )
        owner = self.context["request"].user
        duplicate = Student.objects.filter(
            created_by=owner,
            institution=values["institution"],
            section=values["section"],
            academic_year=values["academic_year"],
            roll_no=values["roll_no"],
        )
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if any(
            normalize_class_name(student.class_name) == normalize_class_name(class_name)
            for student in duplicate
        ):
            raise serializers.ValidationError(
                {
                    "roll_no": "A student with this roll number already exists in this context."
                }
            )
        attrs.update(values)
        registration_no = normalize_student_key(
            attrs.get("registration_no", getattr(self.instance, "registration_no", ""))
        )
        if registration_no:
            duplicate_registration = Student.objects.filter(
                created_by=owner,
                institution=values["institution"],
                registration_no__iexact=registration_no,
            )
            if self.instance:
                duplicate_registration = duplicate_registration.exclude(
                    pk=self.instance.pk
                )
            if duplicate_registration.exists():
                raise serializers.ValidationError(
                    {
                        "registration_no": "A student with this registration number already exists at this institution."
                    }
                )
            attrs["registration_no"] = registration_no
        if not values["roll_no"]:
            raise serializers.ValidationError({"roll_no": "A roll number is required."})
        return attrs
