'''
    Rate-limits by UserId at 2/second (configurable via DRF settings). 
    When the rate is exceeded, raises a DRF Throttled with a friendly message and a Retry-After header.
'''

from rest_framework.throttling import SimpleRateThrottle
from rest_framework.exceptions import Throttled

# NOTE: This throttle is per-UserId across all endpoints using this class.
# To throttle per-endpoint, include view/action name in the cache key.
class UserIdRateThrottle(SimpleRateThrottle):
    # scope ties into REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {'userid': '2/second'}
    scope = 'userid'

    def get_cache_key(self, request, view):
        # Throttle key is the UserId; returns None if header missing so throttling doesn’t run (permission already fails).
        user_id = request.headers.get('UserId') or request.META.get('HTTP_USERID')
        if not user_id:
            # Permission class handles missing header; returning None disables throttling
            return None
        return self.cache_format % {'scope': self.scope, 'ident': user_id}

    def throttle_failure(self):
        # Include wait so DRF sets Retry-After correctly
        raise Throttled(detail="Call Limit Reached", wait=self.wait())
