from rest_framework.permissions import SAFE_METHODS, BasePermission


# Custom permission to allow read-only access, regardless of authentication status.
class IsReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


# Custom permission to only allow owners of an object to access it.
class IsOwner(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        user = request.user
        return (
            getattr(obj, "id", None) == getattr(user, "id", None)
            or getattr(obj, "created_by", None) == user
            or getattr(obj, "user", None) == user
        )


# Custom permission to allow access if the user is not a regular user.
class IsProtected(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and not getattr(
            user, "is_user", False
        )


# Custom permission to allow access if the user is a staff.
class IsStaff(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and getattr(user, "is_staff", False)


# Custom permission to allow access if the user is the owner or an admin.
class IsAdminOrOwner(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return (
            getattr(obj, "id", None) == getattr(user, "id", None)
            or getattr(obj, "created_by", None) == user
            or getattr(obj, "user", None) == user
            or getattr(user, "is_admin", False)
        )


# Custom permission to allow access if the user is an admin.
class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and getattr(user, "is_admin", False)


# Custom permission to allow access if the user is a regular user.
class IsUser(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and getattr(user, "is_user", False)
