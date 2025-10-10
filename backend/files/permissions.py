from rest_framework.permissions import BasePermission

# Enforces the “Every request must be traceable to a user via UserId” requirement.
# A sort of authorization
class HasUserIdHeader(BasePermission):
    # Simple custom permission. Message is what client sees when access is denied.
    message = "Missing required UserId header."

    def has_permission(self, request, view):
        # Supports both UserId: (nice for curl/Fetch) and HTTP_USERID (how Django exposes -H 'UserId: ...' in tests).
        # Returns truthy if present, otherwise False = 403.
        return bool(request.headers.get('UserId') or request.META.get('HTTP_USERID'))
