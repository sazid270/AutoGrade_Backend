from django.urls import include, path
from rest_framework.routers import DefaultRouter

from app.views.exams import ExamView, QuestionView
from app.views.results import ResultView
from app.views.scanning import (
    AnswerSheetView,
    ExamScanConfigurationView,
    ScanBatchView,
    StudentAnswerView,
)
from app.views.students import StudentView

router = DefaultRouter()

# Student url routes
router.register("students", StudentView, basename="student")

# Exam url routes
router.register("exams", ExamView, basename="exam")
router.register("questions", QuestionView, basename="question")

# Result url routes
router.register("results", ResultView, basename="result")
router.register(
    "scan-configurations",
    ExamScanConfigurationView,
    basename="scan-configuration",
)
router.register("batches", ScanBatchView, basename="batch")
router.register("sheets", AnswerSheetView, basename="sheet")
router.register("student-answers", StudentAnswerView, basename="student-answer")

urlpatterns = [path(r"", include(router.urls))]
