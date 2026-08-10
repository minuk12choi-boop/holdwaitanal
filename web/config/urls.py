from django.urls import path

from flowmonitor import views

urlpatterns = [
    path("", views.flowstack, name="flowstack"),
    path("api/flowstack/", views.api_flowstack, name="api_flowstack"),
]
