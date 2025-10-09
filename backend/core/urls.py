"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

"""
Project-level URL routing.

- /admin/ : Django admin (dev tooling)
- /api/   : All API endpoints, delegated to the `files` app.
- static(settings.MEDIA_URL) : Serve uploaded files from MEDIA_ROOT in development.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# Project router matches the /api/… prefix 
# and delegates to the files app’s URL config:
urlpatterns = [
    # 1) Admin site URL (useful for DB inspection in dev)
    path('admin/', admin.site.urls),
    # 2) Delegate all /api/* paths to the files app
    path('api/', include('files.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
