from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django_userforeignkey.models.fields import UserForeignKey


# Mixin to add timestamp fields to a model
class TimeStampMixin(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# Mixin to add a created_by field to a model
class AuthorMixin(models.Model):
    created_by = UserForeignKey(
        auto_user_add=True,
        verbose_name="Created By",
        related_name="%(app_label)s_%(class)s_related",
    )

    class Meta:
        abstract = True


# Mixin to combine AuthorMixin and TimeStampMixin
class AuthorWithTimeStampMixin(AuthorMixin, TimeStampMixin):
    class Meta:
        abstract = True


# Mixin to add an is_active field to a model
class Active(models.Model):
    is_active = models.BooleanField(default=False, blank=True, null=False)

    class Meta:
        abstract = True


# Mixin to add start and end timestamp fields to a model
class StartEndTimeStamp(models.Model):
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()

    class Meta:
        abstract = True


# Mixin to combine AuthorWithTimeStampMixin and Active
class AuthorWithActive(AuthorWithTimeStampMixin, Active):
    class Meta:
        abstract = True


# Mixin to combine AuthorWithActive and StartEndTimeStamp
class AuthorWithActiveStartEndDate(AuthorWithActive, StartEndTimeStamp):
    class Meta:
        abstract = True


# Mixin to create generic relation
class GenericRelation(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=36)
    content_object = GenericForeignKey("content_type", "object_id")

    class Meta:
        abstract = True

    @classmethod
    def delete_for_object(cls, obj):
        content_type = ContentType.objects.get_for_model(obj.__class__)
        cls.objects.filter(content_type=content_type, object_id=obj.id).delete()
