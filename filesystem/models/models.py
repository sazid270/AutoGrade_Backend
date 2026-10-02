from django.db import models
from sorl.thumbnail import ImageField as ThumbnailImageField

from autograde.mixins.models import AuthorWithTimeStampMixin


# File models
class FileModel(AuthorWithTimeStampMixin):
    file = models.FileField(upload_to="files/%Y/%m/%d")


# Image models
class ImageModel(AuthorWithTimeStampMixin):
    image = ThumbnailImageField(upload_to="images/%Y/%m/%d")
