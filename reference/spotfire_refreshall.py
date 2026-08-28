# -*- coding: utf-8 -*-
"""
spotfire_refreshall.py — Spotfire IronPython 스크립트 (RefreshAll)

[등록]
  Edit > Document Properties > Script > New...
    Name     : RefreshAll
    Language : IronPython 2.7
  아래 내용을 그대로 붙여넣는다.

[하는 일]
  1) 원천 테이블을 다시 조회한다(데이터 함수 출력 테이블은 건너뛴다).
  2) S3 적재 데이터 함수를 **정해진 순서로** 실행한다.

[데이터 함수가 셋으로 나뉜 이유]
  한 함수에 14개 표를 다 물리면 Spotfire 가 스크립트를 돌리기 전에
  전부 메모리로 읽다가 죽는다(TIP 1,000만행 + STEP_PATH 700만행).

    s3drive_rest   작은 표 12개
    s3drive_path   PFR1_KFR7_STEP_PATH
    s3drive_tip    PFR1_KFR7_TIP

  큰 것을 먼저 돌리고 rest 를 마지막에 둔다. rest 가 매니페스트를
  완결시키므로 순서가 그래야 읽는 쪽이 중간 상태를 보지 않는다.

[진행 상황]
  문서 속성 runlog 에 남는다. 실패하면 어느 함수가 왜 죽었는지 적힌다.
  Edit > Document Properties > Properties > runlog
"""

from System import DateTime

# 데이터 함수의 출력 테이블. 이것들은 원천이 아니므로 재조회하지 않는다.
#   출력 이름을 바꿨다면 여기도 바꾼다.
SKIP = ["upload_log", "upload_log_tip", "upload_log_path", "upload_log_rest"]

# 실행 순서. 큰 것 먼저, 매니페스트를 완결시키는 rest 를 마지막에.
ORDER = ["s3drive_path", "s3drive_tip", "s3drive_rest"]


def log(msg):
    Document.Properties["runlog"] = DateTime.Now.ToString("HH:mm:ss") + "  " + msg


def run_functions():
    """ORDER 대로 실행하고, 목록에 없는 함수는 그 뒤에 붙여 돌린다."""
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

    done = 0
    bad = []
    for f in todo:
        try:
            log("실행 중 %s" % f.Name)
            f.Execute()
            done += 1
        except Exception, e:          # noqa: E999  (IronPython 2.7 문법)
            # 오류를 삼키면 '완료' 로 찍혀 무엇이 잘못됐는지 알 수 없다.
            bad.append("%s: %s" % (f.Name, str(e)[:120]))
    return done, bad, len(todo)


log("1/2 원천 재조회")
n = 0
bad_t = []
for t in Document.Data.Tables:
    if t.Name in SKIP:
        continue
    try:
        t.Refresh()
        n += 1
    except Exception, e:
        bad_t.append("%s: %s" % (t.Name, str(e)[:80]))

if bad_t:
    log("재조회 실패 %d - %s" % (len(bad_t), " | ".join(bad_t)[:200]))

log("2/2 데이터 함수 실행 (테이블 %d개 재조회)" % n)
done, bad, total = run_functions()

if bad:
    log("실패 %d/%d - %s" % (len(bad), total, " | ".join(bad)[:300]))
else:
    log("완료 (테이블 %d개 · 함수 %d개)" % (n, done))
