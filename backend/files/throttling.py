from rest_framework.throttling import SimpleRateThrottle
from rest_framework.exceptions import Throttled

class UserIdRateThrottle(SimpleRateThrottle):
    scope = 'userid'

    def get_cache_key(self, request, view):
        user_id = request.headers.get('UserId') or request.META.get('HTTP_USERID')
        if not user_id:
            # Permission class handles missing header; returning None disables throttling
            return None
        return self.cache_format % {'scope': self.scope, 'ident': user_id}

    def throttle_failure(self):
        # Include wait so DRF sets Retry-After correctly
        raise Throttled(detail="Call Limit Reached", wait=self.wait())
