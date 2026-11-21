from django.db.models import ProtectedError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.response import Response

from app.models.students import Student
from app.serializers.students import StudentSerializer


# Student views
class StudentView(viewsets.ModelViewSet):
    serializer_class = StudentSerializer
    http_method_names = ["get", "post", "patch", "delete"]
    swagger_tags = ["app_students"]
    filter_backends = (DjangoFilterBackend, filters.SearchFilter)
    filterset_fields = (
        "grade",
        "class_name",
        "section",
        "status",
        "academic_year",
        "institution",
    )
    search_fields = ("name", "roll_no", "email")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Student.objects.none()
        return Student.objects.filter(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "Students with results cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
