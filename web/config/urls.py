from django.contrib.staticfiles.views import serve as static_serve
from django.http import HttpResponse
from django.urls import path, re_path

from flowmonitor import views

urlpatterns = [
    path("", views.flowstack, name="flowstack"),
    path("api/flowstack/", views.api_flowstack, name="api_flowstack"),
    path("api/status/", views.api_status, name="api_status"),
    path("api/lowwt/", views.api_lowwt, name="api_lowwt"),
    path("api/lots/", views.api_lots, name="api_lots"),
    path("api/health/", views.api_health, name="api_health"),
    path("downloads/", views.downloads, name="downloads"),
    path("downloads/wip-raw/", views.download_wip_raw, name="download_wip_raw"),

    # runserver 는 static 을 자동으로 서빙하지만 waitress 등 WSGI 서버는 아니다.
    # chart.umd.js 가 404 나면 화면이 통째로 비므로 항상 서빙되게 둔다.
    # (사내망 전용이라 insecure=True 로 DEBUG=False 에서도 동작시킨다)
    re_path(r"^static/(?P<path>.*)$", static_serve, {"insecure": True}),

    # 브라우저가 자동 요청해 로그를 어지럽히는 것만 막는다
    path("favicon.ico", lambda r: HttpResponse(status=204)),
]
