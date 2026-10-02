from django.contrib.auth.models import AbstractUser, Group
from django.db import models
from django.utils.translation import gettext_lazy as _

from authorization.choices import GENDER_CHOICES, USER_TYPE_CHOICES
from authorization.managers import CustomUserManager
from autograde.mixins.functions import get_custom_uuid
from filesystem.models.models import ImageModel


# User models
class CustomUserModel(AbstractUser):
    id = models.CharField(
        max_length=36, primary_key=True, default=get_custom_uuid("USR"), editable=False
    )
    image = models.ForeignKey(
        ImageModel,
        related_name="user_image",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    username = None
    first_name = models.CharField(max_length=150, blank=True, null=True)
    last_name = models.CharField(max_length=150, blank=True, null=True)
    user_type = models.CharField(
        max_length=10,
        default=USER_TYPE_CHOICES[1][0],
        choices=USER_TYPE_CHOICES,
    )
    email = models.EmailField(
        blank=False,
        null=False,
        unique=True,
        error_messages={
            "unique": _("A user with this email already exists. Try logging in."),
        },
    )
    phone = models.CharField(
        blank=True,
        null=True,
        unique=True,
        error_messages={
            "unique": _("This phone number is already registered."),
        },
    )
    date_of_birth = models.DateField(blank=True, null=True)
    gender = models.CharField(
        max_length=50,
        choices=GENDER_CHOICES,
        blank=True,
        null=True,
    )
    agree_terms = models.BooleanField(default=False)
    verified = models.BooleanField(default=False)

    objects = CustomUserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["user_type"]

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        # Convert empty phone string to None
        if self.phone == "":
            self.phone = None

        super().save(*args, **kwargs)

        # Assign user to the appropriate group based on user_type
        group_name = self.get_user_type_display().lower()
        group, _ = Group.objects.get_or_create(name=group_name)
        if not self.groups.filter(name=group_name).exists():
            self.groups.add(group)

    def get_full_name(self):
        full_name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        return full_name if full_name else "AutoGrade User"

    @property
    def is_admin(self):
        return self.user_type == USER_TYPE_CHOICES[0][0]

    @property
    def is_user(self):
        return self.user_type == USER_TYPE_CHOICES[1][0]

    class Meta:
        unique_together = [["email", "id"]]
