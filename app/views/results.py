from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from app.models.results import Result
from app.serializers.results import ResultSerializer


class ResultView(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ResultSerializer
    swagger_tags = ["app_results"]
    http_method_names = ("get", "head", "options", "patch", "post")
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("exam", "student", "finalized")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Result.objects.none()
        return Result.objects.select_related("exam", "student").filter(
            exam__created_by=self.request.user
        )

    @action(detail=True, methods=("post",))
    def regrade(self, request, pk=None):
        from app.services.grading import regrade_result

        result = self.get_object()
        if result.finalized or result.is_overridden:
            return Response(
                {"detail": "Finalized or overridden results cannot be regraded."},
                status=409,
            )
        result = regrade_result(result.pk)
        return Response(ResultSerializer(result).data)

    @action(detail=True, methods=("post",))
    def reset(self, request, pk=None):
        from app.services.grading import regrade_result

        result = self.get_object()
        if result.finalized:
            return Response(
                {"detail": "Finalized results cannot be reset."},
                status=409,
            )
        result.is_overridden = False
        result.save(update_fields=("is_overridden",))
        result = regrade_result(result.pk)
        return Response(ResultSerializer(result).data)
