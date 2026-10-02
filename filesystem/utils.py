from django.conf import settings
from django.core.files.storage import default_storage


# Get the complete URL for a file field
def get_file_url(file_field):
    if not file_field:
        return None

    return file_field.url


# Delete a file from the configured storage backend
def delete_file_from_storage(file_path):
    try:
        if default_storage.exists(file_path):
            default_storage.delete(file_path)
            return True
        else:
            return False
    except Exception:
        return False


# Get information about the current storage configuration
def get_storage_info():
    return {
        "backend": "local",
        "production": settings.PRODUCTION,
        "media_url": settings.MEDIA_URL,
        "media_root": str(settings.MEDIA_ROOT),
    }


# Validate that local storage is configured
def validate_storage_configuration():
    errors = []

    if not settings.MEDIA_ROOT:
        errors.append("Missing required setting: MEDIA_ROOT")

    return {"valid": len(errors) == 0, "errors": errors}
