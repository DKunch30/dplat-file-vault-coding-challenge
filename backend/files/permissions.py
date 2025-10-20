'''
The following definition is used as an authorization, to check if a request 
includes the proper UserID header.
This integrates with our views.py file, where permissions classes is told 
to look for a user id and return true or false before continuing any other action.
'''
from rest_framework.permissions import BasePermission

# Enforces the “Every request must be traceable to a user via UserId” requirement.
# A sort of authorization, defining custom HasUserIdHeader permission
# check each incoming HTTP request includes UserId header, crucial since we don't use Django traditional username/password or tokens
class HasUserIdHeader(BasePermission):
    # Simple custom permission. Message is what client sees when access is denied.
    message = "Missing required UserId header."

    # Django REST framework automatically calls has_permission() for every incoming request before the view executes
    def has_permission(self, request, view):
        # Supports both UserId: (nice for curl/Fetch) and HTTP_USERID (how Django exposes -H 'UserId: ...' in tests).
        # Returns truthy if present, otherwise False = 403.
        return bool(request.headers.get('UserId') or request.META.get('HTTP_USERID'))
