from authorization.generics.serializers import ProfileSerializer
from authorization.models.models import CustomUserModel


# User serializers
class UserProfileSerializer(ProfileSerializer):
    class Meta:
        model = CustomUserModel
        fields = ProfileSerializer.Meta.fields
        read_only_fields = ProfileSerializer.Meta.read_only_fields
