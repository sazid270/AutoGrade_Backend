from django.urls import include, path
from rest_framework.routers import DefaultRouter

from filesystem.views.views import FileView, ImageView

router = DefaultRouter()

# File url routes
router.register(r"file", FileView, basename="file")

# Image url routes
router.register(r"image", ImageView, basename="image")

urlpatterns = [
    path("", include(router.urls)),
]
