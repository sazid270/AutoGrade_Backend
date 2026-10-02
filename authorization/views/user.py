from rest_framework import permissions

from authorization.choices import USER_TYPE_CHOICES
from authorization.generics.views import ProfileView
from authorization.serializers.user import UserProfileSerializer
from autograde.mixins.functions import create_swagger_schema
from autograde.mixins.permissions import IsOwner, IsUser


# User views
class UserProfileView(ProfileView):
    serializer_class = UserProfileSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        IsOwner & IsUser,
    ]
    profile_type = USER_TYPE_CHOICES[1][0]
    swagger_tags = ["authorization_user"]

    @create_swagger_schema(
        [
            # User Model Filters
            {
                "name": "first_name",
                "description": "Filter by first name (partial match)",
            },
            {"name": "last_name", "description": "Filter by last name (partial match)"},
            {"name": "email", "description": "Filter by email (partial match)"},
            {"name": "phone", "description": "Filter by phone number"},
            {"name": "gender", "description": "Filter by gender"},
            {
                "name": "is_active",
                "description": "Filter by active status. is_active field criteria: True, False",
            },
            {
                "name": "verified",
                "description": "Filter by verified status. verified field criteria: True, False",
            },
            {"name": "date_of_birth", "description": "Filter by date of birth"},
            # Search and Sorting
            {"name": "search", "description": "General search across multiple fields"},
            {
                "name": "sort_by",
                "description": "Sort by field. sort_by field criteria: name_asc, name_desc, email_asc, email_desc, date_joined_asc, date_joined_desc",
            },
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
