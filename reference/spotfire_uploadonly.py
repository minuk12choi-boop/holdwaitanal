# -*- coding: utf-8 -*-
"""
spotfire_uploadonly.py — Spotfire IronPython 스크립트 (UploadOnly)

[등록]
  Edit > Document Properties > Script > New...
    Name     : UploadOnly
    Language : IronPython 2.7
  아래 내용을 그대로 붙여넣는다.

[하는 일]
  등록된 데이터 함수만 실행한다. **원천 재조회는 하지 않는다.**
  지금 문서에 들어 있는 데이터를 그대로 S3 로 올린다.

[언제 쓰나]
  - 컬럼을 추가하는 등 쿼리를 고쳐 이미 한 번 조회해 둔 상태에서,
    재조회 없이 올리기만 하고 싶을 때
  - 업로드가 실패해 다시 올리고 싶을 때

[RefreshAll 과 차이]
  RefreshAll  : 원천 재조회 → 데이터 함수 실행
  UploadOnly  : 데이터 함수 실행만

[진행 상황]
  문서 속성 runlog 에 남는다. 자동 실행기가 이 값을 읽어 '완료' 를
  판정하므로 문구 형식을 RefreshAll 과 같게 맞춘다.
"""

from System import DateTime

# 실행 순서. 큰 것 먼저, 매니페스트를 완결시키는 rest 를 마지막에.
ORDER = ["s3drive_path", "s3drive_tip", "s3drive_rest"]


def log(msg):
    Document.Properties["runlog"] = DateTime.Now.ToString("HH:mm:ss") + "  " + msg


log("업로드만 실행 (원천 재조회 없음)")

byname = {}
for f in Document.Data.DataFunctions:
    byname[f.Name] = f

todo = []
for nm in ORDER:
    if nm in byname:
        todo.append(byname[nm])
for f in Document.Data.DataFunctions:
    if f.Name not in ORDER:
        todo.append(f)

if not todo:
    log("실패: 등록된 데이터 함수가 없습니다")
else:
    n = 0
    bad = []
    for f in todo:
        try:
            log("실행 중 %s" % f.Name)
            f.Execute()
            n += 1
        except Exception, e:          # noqa: E999  (IronPython 2.7 문법)
            # 오류를 삼키면 '완료' 로 찍혀 무엇이 잘못됐는지 알 수 없다.
            bad.append("%s: %s" % (f.Name, str(e)[:120]))

    if bad:
        log("실패 %d/%d - %s" % (len(bad), len(todo), " | ".join(bad)[:300]))
    else:
        # [주의] 자동 실행기가 "완료 (테이블" 을 찾는다. 문구를 맞춘다.
        log("완료 (테이블 %d개, 함수 %d개)" % (n, n))
