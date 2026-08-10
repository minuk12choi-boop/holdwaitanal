from django.urls import path
from flowmonitor import views

urlpatterns = [
    path("", views.flowstack, name="flowstack"),
    path("api/move/", views.api_move, name="api_move"),
]
