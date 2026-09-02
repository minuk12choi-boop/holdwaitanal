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

[조각 수는 여기 적지 않는다]
  s3drive_path_0 · _1 · ... 처럼 이름 뒤에 숫자가 붙은 함수를 문서에서
  찾아 번호 순으로 돌린다. 3등분을 6등분으로 바꿔도 고칠 필요가 없다.

    s3drive_path_*   STEP_PATH 조각
    s3drive_tip_*    TIP 조각
    s3drive_rest     작은 표들 (마지막에 돈다)

  rest 를 마지막에 두는 이유는 그것이 매니페스트를 완결시키기 때문이다.

[진행 상황]
  문서 속성 runlog 에 남는다.
    Edit > Document Properties > Properties > runlog
  '완료' 는 **함수가 예외 없이 끝났다** 는 뜻일 뿐이다. 업로드 성공은
  upload_log_* 표의 status 로 확인한다.
"""

from System import DateTime

PREFIX_ORDER = ["s3drive_path_", "s3drive_tip_"]   # 숫자가 붙는 조각 함수
LAST = ["s3drive_rest"]                            # 맨 뒤에 돌 함수


def log(msg):
    Document.Properties["runlog"] = DateTime.Now.ToString("HH:mm:ss") + "  " + msg


def num_of(name, prefix):
    """s3drive_tip_10 -> 10. 숫자가 아니면 -1."""
    tail = name[len(prefix):]
    try:
        return int(tail)
    except ValueError:
        return -1


def plan_order(funcs):
    """조각은 번호 순으로, rest 는 맨 뒤로."""
    todo = []
    used = {}
    for pre in PREFIX_ORDER:
        got = []
        for f in funcs:
            if f.Name.startswith(pre) and num_of(f.Name, pre) >= 0:
                got.append(f)
        got.sort(key=lambda x: num_of(x.Name, pre))
        for f in got:
            todo.append(f)
            used[f.Name] = 1
    for f in funcs:                      # 목록에 없는 함수는 가운데
        if f.Name not in used and f.Name not in LAST:
            todo.append(f)
            used[f.Name] = 1
    for nm in LAST:
        for f in funcs:
            if f.Name == nm and f.Name not in used:
                todo.append(f)
                used[f.Name] = 1
    return todo


log("업로드만 실행 (원천 재조회 없음)")

funcs = []
for f in Document.Data.DataFunctions:
    funcs.append(f)
todo = plan_order(funcs)

if not todo:
    log("실패: 등록된 데이터 함수가 없습니다")
else:
    # 조각이 몇 개인지 먼저 남긴다. 등록을 빠뜨리면 여기서 드러난다.
    counts = []
    for pre in PREFIX_ORDER:
        c = 0
        for f in funcs:
            if f.Name.startswith(pre) and num_of(f.Name, pre) >= 0:
                c += 1
        counts.append("%s%d개" % (pre, c))
    log("함수 %d개 (%s)" % (len(todo), " · ".join(counts)))

    total = len(todo)
    ok = 0
    bad = []
    for f in todo:
        try:
            log("실행 중 %d/%d %s" % (ok + len(bad) + 1, total, f.Name))
            f.Execute()
            ok += 1
        except Exception, e:          # noqa: E999  (IronPython 2.7 문법)
            # 오류를 삼키면 '완료' 로 찍혀 무엇이 잘못됐는지 알 수 없다.
            # 하나가 실패해도 나머지는 계속 올린다.
            bad.append("%s: %s" % (f.Name, str(e)[:120]))

    if bad:
        log("실패 %d/%d - %s" % (len(bad), total, " | ".join(bad)[:300]))
    else:
        # [주의] 자동 실행기가 "완료 (테이블" 을 찾는다. 앞부분을 바꾸지 않는다.
        log("완료 (테이블 적재결과는 upload_log 확인 · 함수 %d개)" % ok)
