'''
    Rate-limits by UserId at 2/second (configurable via DRF settings.py). 
    When the rate is exceeded, raises a DRF Throttled with a friendly message and a Retry-After header.

    This definition utilizes the backend/core/settings.py USERID_THROTTLE_RATE
    provided in the REST_FRAMEWORK to determine the rate limit.

    This definition is then called on in the views.py file via the throttle_classes 
    variable. Which will then apply the UserIdRateThrottle to all actions in this viewset.
    So now, every GET, POST, or DELETE call to /api/files/ is limited per user.

    Throttling is a safety mechanism for our API. It prevents a single user from:
    - Flooding our server with too many requests,
    - Causing slowdowns for everyone else,
    - Accidentally spamming our system in a loop.

    We can think of it as a per-user speed limit.
'''

from rest_framework.throttling import SimpleRateThrottle
from rest_framework.exceptions import Throttled

# This throttle is per-UserId across all endpoints using this class.
# If we want to throttle per-endpoint, include view/action name in the cache key.
class UserIdRateThrottle(SimpleRateThrottle):
    # scope ties into REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {'userid': '2/second'}
    # Django REST framework uses this to look up the rate limit in backend/core/settings.py
    scope = 'userid'

    def get_cache_key(self, request, view):
        # Throttle key is the UserId; returns None if header missing so throttling doesn’t run (permission already fails).
        # Extracting UserId from header
        user_id = request.headers.get('UserId') or request.META.get('HTTP_USERID')
        # scope = getattr(view, "throttle_scope", None)
        if not user_id:
            # Permission class handles missing header; returning None disables throttling
            return None
        # DRF stores throttle counters in its cache (like “user X made 3 requests this second”).
        return self.cache_format % {'scope': self.scope, 'ident': user_id}

    # Overrides DRF’s default failure behavior
    def throttle_failure(self):
        # Include wait so DRF sets Retry-After correctly
        raise Throttled(detail="Call Limit Reached", wait=self.wait())
