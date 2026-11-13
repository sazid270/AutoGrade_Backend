from django.contrib import admin

from omr.models import Assessment, ProcessJob, Sheet, SheetResult


@admin.register(Assessment)
class AssessmentAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "subject", "status", "created_by", "created_at")
    search_fields = ("title", "subject")


@admin.register(Sheet)
class SheetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "assessment",
        "roll_no",
        "roll_confidence",
        "status",
        "created_at",
    )
    list_filter = ("status",)


@admin.register(SheetResult)
class SheetResultAdmin(admin.ModelAdmin):
    list_display = ("id", "sheet", "total", "percentage", "grade", "needs_review_count")


@admin.register(ProcessJob)
class ProcessJobAdmin(admin.ModelAdmin):
    list_display = ("id", "assessment", "status", "stage_index", "created_at")
    list_filter = ("status",)
