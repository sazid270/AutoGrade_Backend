from django.contrib.auth.models import UserManager

from authorization.choices import USER_TYPE_CHOICES


class CustomUserManager(UserManager):
    # Create and save a user with the given email and password.
    def create_user(self, email, phone=None, password=None, **extra_fields):
        user_type = extra_fields.get("user_type", USER_TYPE_CHOICES[1][0])
        is_user = user_type == USER_TYPE_CHOICES[1][0]
        extra_fields.setdefault("is_staff", not is_user)
        if user_type == USER_TYPE_CHOICES[0][0]:
            extra_fields.setdefault("is_superuser", True)
        else:
            extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, phone, password, **extra_fields)

    # Create and save a superuser with the given email and password.
    def create_superuser(self, email, phone=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("user_type", USER_TYPE_CHOICES[0][0])
        extra_fields.setdefault("agree_terms", True)
        extra_fields.setdefault("verified", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, phone, password, **extra_fields)

    # Create a user with the given email and password.
    def _create_user(self, email, phone=None, password=None, **extra_fields):
        if not email:
            raise ValueError("The given email must be set")
        email = self.normalize_email(email)
        phone = phone or None
        user = self.model(email=email, phone=phone, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user
