from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import logout
from django.http import HttpResponseRedirect
from django.urls import include, path
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions

from .index import health_check, index

schema_view = get_schema_view(
    openapi.Info(
        title="AutoGrade APIs",
        default_version="v1",
        description="AutoGrade's APIs provides scalable and efficient endpoints to power the AutoGrade platform, enabling seamless data management and communication between services.",
        terms_of_service="https://www.google.com/policies/terms/",
        contact=openapi.Contact(email="contact@snippets.local"),
        license=openapi.License(name="BSD License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)


def django_logout(request):
    logout(request)
    next_url = request.GET.get("next", "/")
    return HttpResponseRedirect(next_url)


urlpatterns = [
    # Root endpoints
    path("", index, name="index"),
    path("health/", health_check, name="health_check"),
    # API Documentation
    path(
        "api-docs/",
        schema_view.with_ui("swagger", cache_timeout=0),
        name="schema-swagger-ui",
    ),
    path("api-auth/logout/", django_logout, name="django_logout"),
    # Admin endpoints
    path("admin/", admin.site.urls, name="admin"),
    # API endpoints
    path("api/authentication/", include("authentication.urls")),
    path("api/authorization/", include("authorization.urls")),
    path("api/app/", include("app.urls")),
    path("api/file/", include("filesystem.urls")),
    path("api-auth/", include("rest_framework.urls")),
]


if settings.DEBUG:
    urlpatterns = (
        urlpatterns
        + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
        + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    )
