from rest_framework import serializers

from app.models.students import Student
from app.utils.student_keys import normalize_roll_no, normalize_student_key


## Student serializers
class StudentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Student
        fields = "__all__"
        read_only_fields = ("created_by", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        for field in ("institution", "grade", "section", "academic_year"):
            validated_data[field] = normalize_student_key(validated_data.get(field))
        validated_data["roll_no"] = normalize_roll_no(validated_data.get("roll_no"))
        if "registration_no" in validated_data:
            validated_data["registration_no"] = normalize_student_key(
                validated_data["registration_no"]
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        for field in ("institution", "grade", "section", "academic_year"):
            if field in validated_data:
                validated_data[field] = normalize_student_key(validated_data[field])
        if "roll_no" in validated_data:
            validated_data["roll_no"] = normalize_roll_no(validated_data["roll_no"])
        if "registration_no" in validated_data:
            validated_data["registration_no"] = normalize_student_key(
                validated_data["registration_no"]
            )
        return super().update(instance, validated_data)

    def validate(self, attrs):
        values = {
            field: normalize_roll_no(
                attrs.get(field, getattr(self.instance, field, ""))
            )
            if field == "roll_no"
            else normalize_student_key(
                attrs.get(field, getattr(self.instance, field, ""))
            )
            for field in (
                "institution",
                "grade",
                "section",
                "academic_year",
                "roll_no",
            )
        }
        owner = self.context["request"].user
        duplicate = Student.objects.filter(created_by=owner, **values)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
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
