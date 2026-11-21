from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets

from app.models.results import Result
from app.serializers.results import ResultSerializer


# Result views
class ResultView(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ResultSerializer
    swagger_tags = ["app_results"]
    http_method_names = ["get", "patch"]
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("exam", "student", "finalized")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Result.objects.none()
        return Result.objects.select_related("exam", "student").filter(
            exam__created_by=self.request.user
        )
