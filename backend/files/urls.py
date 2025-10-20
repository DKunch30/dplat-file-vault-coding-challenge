"""
App-level URL routing for the File Vault API.

We use a DRF router to register FileViewSet, which auto-generates:
- /api/files/            [GET=list, POST=create]
- /api/files/{id}/       [GET=retrieve, DELETE=destroy]
- /api/files/storage_stats/  [GET]  (from @action(detail=False))
- /api/files/file_types/     [GET]  (from @action(detail=False))

Note: The '/api/' prefix is added by the project router in core/urls.py:
    path('api/', include('files.urls'))
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import FileViewSet

# DefaultRouter creates the standard REST routes for a ViewSet and also
# exposes any @action methods you define on the ViewSet.
router = DefaultRouter()

# From this single line, DRF auto-generates the routes listed above.
router.register(r'files', FileViewSet)

# Expose all router-generated URLs under this app.
urlpatterns = [
    path('', include(router.urls)),
] 