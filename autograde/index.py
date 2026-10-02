from django.http import JsonResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response


# Root endpoint for the AutoGrade API
def index(request):
    return JsonResponse(
        {
            "message": "Welcome to AutoGrade API",
            "version": "1.0.0",
            "status": "running",
            "endpoints": {
                "api_docs": "/api-docs/",
                "admin": "/admin/",
            },
        }
    )


@api_view(["GET"])
# Health check endpoint
def health_check(request):
    return Response({"status": "healthy", "message": "Server is running properly"})
