from django.contrib import admin
from sorl.thumbnail.admin import AdminImageMixin

from filesystem.models.models import FileModel, ImageModel


class ItemAdmin(AdminImageMixin, admin.ModelAdmin):
    pass


admin.site.register(FileModel)
admin.site.register(ImageModel, ItemAdmin)
