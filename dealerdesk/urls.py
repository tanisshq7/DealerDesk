from django.contrib import admin
from django.urls import include, path

from core import views as core_views

urlpatterns = [
    path("", core_views.portal_view, name="portal"),
    path("portal/", core_views.portal_view, name="portal_alt"),
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
]
