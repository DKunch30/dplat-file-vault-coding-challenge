from rest_framework.permissions import BasePermission

# Enforces the “Every request must be traceable to a user via UserId” requirement.
class HasUserIdHeader(BasePermission):
    message = "Missing required UserId header."

    def has_permission(self, request, view):
        return bool(request.headers.get('UserId') or request.META.get('HTTP_USERID'))
