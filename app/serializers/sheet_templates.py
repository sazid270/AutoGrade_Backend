from rest_framework import serializers

from app.models.sheet_templates import SheetTemplate
from filesystem.models.models import ImageModel


class SheetTemplateSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    image_id = serializers.IntegerField(source="reference_image_id", read_only=True)
    structure_summary = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = SheetTemplate
        fields = (
            "id",
            "name",
            "image_id",
            "image_url",
            "structure_summary",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_image_url(self, obj):
        return obj.reference_image.image.url if obj.reference_image.image else None

    def get_structure_summary(self, obj):
        structure = obj.structure
        fields = structure.get("fields", {})
        return {
            "questions": len(structure.get("options_per_question", [])),
            "options_per_question": structure.get("options_per_question", []),
            "numbering": structure.get("numbering"),
            "roll_digits": len(fields.get("roll", {}).get("digits", [])),
            "registration_digits": len(
                fields.get("registration", {}).get("digits", [])
            ),
            "canvas": structure.get("canvas"),
            "grids": structure.get("grids", {}),
        }

    def get_status(self, _obj):
        return "ready"


class SheetTemplateCreateSerializer(serializers.Serializer):
    name = serializers.CharField(
        max_length=255,
        help_text="A name for this answer-sheet layout.",
    )
    image_id = serializers.PrimaryKeyRelatedField(
        source="reference_image",
        queryset=ImageModel.objects.all(),
        help_text="Image ID returned by POST /api/file/image/.",
    )

    def validate_image_id(self, value):
        if value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError(
                "The reference image does not belong to your account."
            )
        return value


class SheetTemplatePatchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    numbering = serializers.ChoiceField(
        choices=("column", "row"),
        required=False,
    )
    image_id = serializers.PrimaryKeyRelatedField(
        source="reference_image",
        queryset=ImageModel.objects.all(),
        required=False,
    )

    def validate_image_id(self, value):
        if value.created_by_id != self.context["request"].user.id:
            raise serializers.ValidationError(
                "The reference image does not belong to your account."
            )
        return value
