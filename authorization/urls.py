from django.urls import include, path
from rest_framework import routers

from authorization.views.admin import AdminProfileView, ProfileActivationView
from authorization.views.user import UserProfileView

router = routers.DefaultRouter()

router.register(r"admin-profile", AdminProfileView, basename="admin-profile")
router.register(r"user-profile", UserProfileView, basename="user-profile")

urlpatterns = [
    path(r"", include(router.urls)),
    path(
        "profile-activation/<str:pk>/",
        ProfileActivationView.as_view(),
        name="profile-activation",
    ),
]
