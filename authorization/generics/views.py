from django.db.models import Q
from rest_framework import authentication, permissions
from rest_framework_simplejwt import authentication as jwt_authentication

from authorization.models.models import CustomUserModel
from autograde.mixins.permissions import IsAdmin, IsAdminOrOwner
from autograde.mixins.viewsets import BaseModelViewSet


# Base profile view for different user types
class ProfileView(BaseModelViewSet):
    queryset = CustomUserModel.objects.all()
    serializer_class = None
    http_method_names = ["get", "patch", "delete"]
    authentication_classes = [
        jwt_authentication.JWTAuthentication,
        authentication.SessionAuthentication,
    ]
    permission_classes = None
    profile_type = None  # Type of user

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        params = self.request.query_params

        if not user.is_authenticated:
            return queryset.none()

        # Base queryset with permission logic
        if self.action == "list":
            if user.is_admin:
                queryset = queryset.filter(user_type=self.profile_type)
            else:
                self.permission_denied(
                    self.request, message="Not authorized to view this list"
                )
        elif self.action == "retrieve":
            if str(user.id) == str(self.kwargs.get("pk")):
                queryset = queryset.filter(
                    id=self.kwargs.get("pk"), user_type=self.profile_type
                )
            elif user.is_admin:
                queryset = queryset.filter(
                    id=self.kwargs.get("pk"), user_type=self.profile_type
                )
            else:
                self.permission_denied(
                    self.request, message="Not authorized to view this profile"
                )
        else:
            queryset = queryset.filter(id=user.id, user_type=self.profile_type)

        # User Model Filters
        if params and params.get("first_name"):
            queryset = queryset.filter(first_name__icontains=params["first_name"])
        if params and params.get("last_name"):
            queryset = queryset.filter(last_name__icontains=params["last_name"])
        if params and params.get("email"):
            queryset = queryset.filter(email__icontains=params["email"])
        if params and params.get("phone"):
            phone = params["phone"].strip().lstrip("+").lstrip("88").lstrip("0")
            queryset = queryset.filter(phone__icontains=phone)
        if params and params.get("gender"):
            queryset = queryset.filter(gender=params["gender"])
        if params and params.get("is_active") is not None:
            is_active = params["is_active"].lower() == "true"
            queryset = queryset.filter(is_active=is_active)
        if params and params.get("verified") is not None:
            verified = params["verified"].lower() == "true"
            queryset = queryset.filter(verified=verified)
        if params and params.get("date_of_birth"):
            queryset = queryset.filter(date_of_birth=params["date_of_birth"])

        search = params.get("search") if params else None
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
                | Q(
                    phone__icontains=search.strip().lstrip("+").lstrip("88").lstrip("0")
                )
                | Q(gender__icontains=search)
                | Q(is_active__icontains=search)
                | Q(verified__icontains=search)
                | Q(date_of_birth__icontains=search)
            )

        # Sorting
        sort_by = params.get("sort_by") if params else None
        if sort_by == "name_asc":
            queryset = queryset.order_by("first_name", "last_name")
        elif sort_by == "name_desc":
            queryset = queryset.order_by("-first_name", "-last_name")
        elif sort_by == "email_asc":
            queryset = queryset.order_by("email")
        elif sort_by == "email_desc":
            queryset = queryset.order_by("-email")
        elif sort_by == "date_joined_asc":
            queryset = queryset.order_by("date_joined")
        elif sort_by == "date_joined_desc":
            queryset = queryset.order_by("-date_joined")
        else:
            queryset = queryset.order_by("-date_joined")

        return queryset.distinct()

    def get_permissions(self):
        if self.action == "list":
            self.permission_classes = [
                permissions.IsAuthenticated,
                IsAdmin,
            ]
        if self.action == "retrieve":
            self.permission_classes = [
                permissions.IsAuthenticated,
                IsAdminOrOwner,
            ]
        return [permission() for permission in self.permission_classes]
