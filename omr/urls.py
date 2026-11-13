from django.urls import path

from omr.views.views import (
    AssessmentAddSheets,
    AssessmentAnalytics,
    AssessmentDetail,
    AssessmentExport,
    AssessmentListCreate,
    AssessmentReprocess,
    JobDetail,
    SheetDetail,
    SheetVerify,
)

urlpatterns = [
    path("assessments/", AssessmentListCreate.as_view(), name="omr-assessments"),
    path(
        "assessments/<int:pk>/",
        AssessmentDetail.as_view(),
        name="omr-assessment-detail",
    ),
    path(
        "assessments/<int:pk>/sheets/",
        AssessmentAddSheets.as_view(),
        name="omr-assessment-sheets",
    ),
    path(
        "assessments/<int:pk>/reprocess/",
        AssessmentReprocess.as_view(),
        name="omr-assessment-reprocess",
    ),
    path(
        "assessments/<int:pk>/analytics/",
        AssessmentAnalytics.as_view(),
        name="omr-assessment-analytics",
    ),
    path(
        "assessments/<int:pk>/export/",
        AssessmentExport.as_view(),
        name="omr-assessment-export",
    ),
    path("jobs/<int:pk>/", JobDetail.as_view(), name="omr-job-detail"),
    path("sheets/<int:pk>/", SheetDetail.as_view(), name="omr-sheet-detail"),
    path(
        "sheets/<int:pk>/verify/",
        SheetVerify.as_view(),
        name="omr-sheet-verify",
    ),
]
