from rest_framework import generics, viewsets

from autograde.mixins.classes import CustomPagePagination, SwaggerFakeView


class BaseGenericViewSet(SwaggerFakeView, viewsets.GenericViewSet):
    pass


class BaseReadOnlyModelViewSet(SwaggerFakeView, viewsets.ReadOnlyModelViewSet):
    pagination_class = CustomPagePagination


class BaseModelViewSet(SwaggerFakeView, viewsets.ModelViewSet):
    pagination_class = CustomPagePagination


class BaseCreateAPIView(SwaggerFakeView, generics.CreateAPIView):
    pass


class BaseListAPIView(SwaggerFakeView, generics.ListAPIView):
    pagination_class = CustomPagePagination


class BaseRetrieveAPIView(SwaggerFakeView, generics.RetrieveAPIView):
    pass


class BaseDestroyAPIView(SwaggerFakeView, generics.DestroyAPIView):
    pass


class BaseUpdateAPIView(SwaggerFakeView, generics.UpdateAPIView):
    pass


class BaseListCreateAPIView(SwaggerFakeView, generics.ListCreateAPIView):
    pagination_class = CustomPagePagination


class BaseRetrieveUpdateAPIView(SwaggerFakeView, generics.RetrieveUpdateAPIView):
    pass


class BaseRetrieveDestroyAPIView(SwaggerFakeView, generics.RetrieveDestroyAPIView):
    pass


class BaseRetrieveUpdateDestroyAPIView(
    SwaggerFakeView, generics.RetrieveUpdateDestroyAPIView
):
    pass
