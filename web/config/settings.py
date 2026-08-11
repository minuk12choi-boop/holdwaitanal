# -*- coding: utf-8 -*-
"""Django 설정. .env 의 HOLDWAITANAL_* / DJANGO_* 를 읽는다."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent       # web/
ROOT_DIR = BASE_DIR.parent                              # 저장소 루트
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "getdata"))           # db_common 재사용

import db_common as DB  # noqa: E402
DB.load_env()                                           # .env 는 루트에서 자동 탐색

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() == "true"
ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
                 if h.strip()]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "flowmonitor",
]
MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
ROOT_URLCONF = "config.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("HOLDWAITANAL_DB_NAME", "app_db"),
        "USER": os.environ.get("HOLDWAITANAL_DB_USER", ""),
        "PASSWORD": os.environ.get("HOLDWAITANAL_DB_PASSWORD", ""),
        "HOST": os.environ.get("HOLDWAITANAL_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("HOLDWAITANAL_DB_PORT", "3306"),
        "OPTIONS": {"charset": os.environ.get("HOLDWAITANAL_DB_CHARSET", "utf8mb4")},
    }
}

STATIC_URL = "static/"
USE_TZ = False
TIME_ZONE = "Asia/Seoul"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass
