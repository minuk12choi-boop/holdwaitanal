# -*- coding: utf-8 -*-
"""
spotfire_refreshall.py — Spotfire IronPython 스크립트 (RefreshAll)

[등록]
  Edit > Document Properties > Script > New...
    Name     : RefreshAll
    Language : IronPython 2.7
  아래 내용을 그대로 붙여넣는다.

[하는 일]
  1) 원천 데이터 테이블 재조회
  2) 등록된 데이터 함수 전부 실행 (S3 업로드)
  진행 상황은 문서 속성 runlog 에 남긴다.

[주의]
  - 데이터 함수의 출력 테이블(upload_log 등)은 Refresh 대상에서 뺀다.
    건드리면 함수가 중복 실행된다.
  - 데이터 함수를 여러 개로 나눠 등록했어도 DataFunctions 를 전부 돌므로
    이 스크립트는 그대로 두면 된다.
  - Document.Data.Reload() 는 이 버전에 없다. 테이블별 Refresh() 를 쓴다.
"""

from System import DateTime

# 데이터 함수의 출력 테이블. 이름이 다르면 여기에 맞춰 고친다.
SKIP = ["upload_log"]


def log(msg):
    Document.Properties["runlog"] = DateTime.Now.ToString("HH:mm:ss") + "  " + msg


log("1/2 원천 재조회")
n = 0
for t in Document.Data.Tables:
    if t.Name in SKIP:
        continue
    try:
        t.Refresh()
        n += 1
    except:
        pass

log("2/2 데이터 함수 실행 (%d개 테이블 재조회 완료)" % n)
for f in Document.Data.DataFunctions:
    try:
        f.Execute()
    except:
        pass

log("완료 (테이블 %d개)" % n)
