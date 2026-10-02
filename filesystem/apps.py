from importlib import import_module

from django.apps import AppConfig


class FilesystemConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "filesystem"

    def ready(self):
        _ = import_module("filesystem.signals")
