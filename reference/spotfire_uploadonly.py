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

[데이터 함수가 일곱인 이유]
  한 함수에 큰 표를 물리면 Spotfire 가 스크립트를 돌리기 전에 전부
  메모리로 읽다가 죽는다. STEP_PATH 와 TIP 을 셋씩 갈랐다.

    s3drive_path_0/1/2   STEP_PATH 조각
    s3drive_tip_0/1/2    TIP 조각
    s3drive_rest         작은 표 13개

  rest 를 마지막에 둔다. 그것이 매니페스트를 완결시키므로, 읽는 쪽이
  중간 상태를 최신으로 오인하지 않는다.

[진행 상황]
  문서 속성 runlog 에 남는다.
    Edit > Document Properties > Properties > runlog
  자동 실행기가 "완료 (테이블" 을 찾아 끝난 줄 아므로 문구를 바꾸지 않는다.
"""

from System import DateTime

# 실행 순서. 큰 것 먼저, 매니페스트를 완결시키는 rest 를 마지막에.
#   여기 없는 함수는 뒤에 붙여 함께 돌린다(이름을 바꿔도 빠지지 않는다).
ORDER = ["s3drive_path_0", "s3drive_path_1", "s3drive_path_2",
         "s3drive_tip_0", "s3drive_tip_1", "s3drive_tip_2",
         "s3drive_rest"]


def log(msg):
    Document.Properties["runlog"] = DateTime.Now.ToString("HH:mm:ss") + "  " + msg


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

# ORDER 에 적힌 함수가 문서에 없으면 미리 알린다.
#   이름을 잘못 등록하면 조용히 빠져 그 표만 옛 데이터가 남는다.
absent = []
for nm in ORDER:
    if nm not in byname:
        absent.append(nm)

if not todo:
    log("실패: 등록된 데이터 함수가 없습니다")
elif absent:
    log("주의: 없는 함수 %s - 나머지 %d개만 실행합니다"
        % (", ".join(absent), len(todo)))

if todo:
    total = len(todo)
    n = 0
    bad = []
    for f in todo:
        try:
            # 몇 번째인지 함께 남긴다. 어디서 멈췄는지 바로 보인다.
            log("실행 중 %d/%d %s" % (n + len(bad) + 1, total, f.Name))
            f.Execute()
            n += 1
        except Exception, e:          # noqa: E999  (IronPython 2.7 문법)
            # 오류를 삼키면 '완료' 로 찍혀 무엇이 잘못됐는지 알 수 없다.
            # 하나가 실패해도 나머지는 계속 올린다.
            bad.append("%s: %s" % (f.Name, str(e)[:120]))

    if bad:
        log("실패 %d/%d - %s" % (len(bad), total, " | ".join(bad)[:300]))
    else:
        # [주의] 자동 실행기가 "완료 (테이블" 을 찾는다. 문구를 바꾸지 않는다.
        log("완료 (테이블 %d개, 함수 %d개)" % (n, n))
