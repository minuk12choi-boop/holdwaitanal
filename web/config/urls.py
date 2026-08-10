from django.urls import path

from flowmonitor import views

urlpatterns = [
    path("", views.flowstack, name="flowstack"),
    path("api/flowstack/", views.api_flowstack, name="api_flowstack"),
    path("api/status/", views.api_status, name="api_status"),
    path("api/lowwt/", views.api_lowwt, name="api_lowwt"),
    path("api/health/", views.api_health, name="api_health"),
    path("downloads/", views.downloads, name="downloads"),
    path("downloads/wip-raw/", views.download_wip_raw, name="download_wip_raw"),
]
