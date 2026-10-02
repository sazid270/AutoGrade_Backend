from rest_framework import authentication, permissions, status
from rest_framework.response import Response
from rest_framework_simplejwt import authentication as jwt_authentication

from authorization.choices import USER_TYPE_CHOICES
from authorization.generics.views import ProfileView
from authorization.models.models import CustomUserModel
from authorization.serializers.admin import (
    AdminProfileSerializer,
    ProfileActivationSerializer,
)
from autograde.mixins.functions import create_swagger_schema
from autograde.mixins.permissions import IsAdmin, IsOwner
from autograde.mixins.viewsets import BaseUpdateAPIView


# Admin views
class AdminProfileView(ProfileView):
    serializer_class = AdminProfileSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        IsOwner & IsAdmin,
    ]
    profile_type = USER_TYPE_CHOICES[0][0]
    swagger_tags = ["authorization_admin"]

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


# Profile management views
class ProfileActivationView(BaseUpdateAPIView):
    queryset = CustomUserModel.objects.all()
    serializer_class = ProfileActivationSerializer
    http_method_names = ["put"]
    authentication_classes = [
        jwt_authentication.JWTAuthentication,
        authentication.SessionAuthentication,
    ]
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    swagger_tags = ["authorization_profile_management"]

    def put(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user

        # Prevent users from modifying their own is_active state
        if user.id == instance.id and "is_active" in request.data:
            return Response(
                {"detail": "You cannot modify your own account status."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data, status=status.HTTP_200_OK)
