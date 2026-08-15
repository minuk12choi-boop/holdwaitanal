from django.contrib.staticfiles.views import serve as static_serve
from django.http import HttpResponse
from django.urls import path, re_path
from django.views.generic import RedirectView

from flowmonitor import views

urlpatterns = [
    path("", RedirectView.as_view(url="/main/", permanent=False)),
    path("main/", views.fab_status, name="fab_status"),
    path("metrics/", views.fab_metrics, name="fab_metrics"),   # 메뉴 미노출
    path("master/", views.standards, name="standards"),
    # 예전 주소로 들어와도 끊기지 않게
    path("standards/", RedirectView.as_view(url="/master/", permanent=False)),
    path("downloads/", views.downloads, name="downloads"),
    path("downloads/wip-raw/", views.download_wip_raw, name="download_wip_raw"),

    path("api/summary/", views.api_summary, name="api_summary"),
    path("api/flowstack/", views.api_flowstack, name="api_flowstack"),
    path("api/status/", views.api_status, name="api_status"),
    path("api/lowwt/", views.api_lowwt, name="api_lowwt"),
    path("api/lots/", views.api_lots, name="api_lots"),
    path("api/lots-live/", views.api_lots_live, name="api_lots_live"),
    path("api/lot-steps/", views.api_lot_steps, name="api_lot_steps"),
    path("api/health/", views.api_health, name="api_health"),

    # runserver 는 static 을 자동 서빙하지만 waitress 등 WSGI 서버는 아니다.
    re_path(r"^static/(?P<path>.*)$", static_serve, {"insecure": True}),
    path("favicon.ico", lambda r: HttpResponse(status=204)),
]
