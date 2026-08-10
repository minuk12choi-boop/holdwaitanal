#!/usr/bin/env python
"""Django 관리 스크립트.

runserver 를 포트 지정 없이 실행하면 DEFAULT_PORT 를 쓴다.
8000 / 9001 / 9002 는 다른 웹이 점유 중이라 피한다.
포트를 바꾸려면 .env 의 DJANGO_PORT 또는 인자로 직접 지정.
"""
import os
import sys

DEFAULT_PORT = "8010"

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        import db_common
        db_common.load_env()
    except Exception:
        pass

    argv = sys.argv
    if len(argv) > 1 and argv[1] == "runserver" and len(argv) == 2:
        argv = argv + [f"0.0.0.0:{os.environ.get('DJANGO_PORT', DEFAULT_PORT)}"]

    from django.core.management import execute_from_command_line
    execute_from_command_line(argv)
