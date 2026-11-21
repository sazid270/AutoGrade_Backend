from django.urls import include, path
from rest_framework.routers import DefaultRouter

from app.views.exams import ExamView, QuestionView
from app.views.results import ResultView
from app.views.scanning import (
    AnswerSheetBatchView,
    AnswerSheetView,
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
    "answer-sheet-batch",
    AnswerSheetBatchView,
    basename="answer-sheet-batch",
)
router.register("answer-sheets", AnswerSheetView, basename="answer-sheet")

urlpatterns = [path(r"", include(router.urls))]
