# 작업 스케줄러 설정

## 등록할 작업은 1개뿐

| 항목 | 값 |
|---|---|
| 프로그램/스크립트 | `D:\PERSONAL_SPACE\SW\python\7_holdwaitanal\scripts\run_pipeline.bat` |
| 인수 추가 | (비워둠) |
| 시작 위치 | `D:\PERSONAL_SPACE\SW\python\7_holdwaitanal` |

**트리거**

```
매일  시작 22:00
고급 설정 > 작업 반복 간격 : 30분,  기간 : 1일
```

**설정 탭**

```
[v] 작업이 이미 실행 중이면 다음 규칙 적용 : 새 인스턴스 시작 안 함
```

겹침 방지용이다. 한 회차가 20~30초라 보통 겹치지 않지만 안전장치로 둔다.

## run_pipeline.bat 이 하는 일

```
1) getdata\build_f3.py    f3_live / f3_history
2) getdata\get_move.py    move_shift / move_daily / move_lot
```

**두 개를 한 파일에서 순서대로 돌린다.** 스케줄러에 따로 등록하지 않는다.

로그는 `logs\pipeline.log` 한 곳에 쌓인다.

```
===== 2026-08-14 10:15:01 =====
[ENV] python=C:\Users\...\aipforge\python.exe
--- build_f3 ---
...
[EXIT] build_f3 =0
--- get_move ---
...
[EXIT] get_move =0
```

`[EXIT] ... =0` 이 아니면 그 단계가 실패한 것이다.

## 수동 실행

같은 파일에 인수를 붙이면 된다.

```
scripts\run_pipeline.bat --force              새 회차가 아니어도 build_f3 강제 실행
scripts\run_pipeline.bat --move-only --full   MOVE 만 3개월 재적재
scripts\run_pipeline.bat --move-only --hours 6
scripts\run_pipeline.bat --f3-only
```

## 기존 작업 정리

이전에 `run_build_f3.bat` 을 등록해 두었다면 **그 작업의 프로그램 경로만
`run_pipeline.bat` 으로 바꾸면 된다.** 트리거는 30분 간격으로 맞춘다.

아래 파일들은 삭제했다. 스케줄러에 남아 있으면 지운다.

```
scripts\run_build_f3.bat        -> run_pipeline.bat 으로 통합
scripts\run_get_move.bat        -> run_pipeline.bat --move-only
scripts\_find_python.bat        -> run_pipeline.bat 안에 흡수
scripts\run_spotfire_refresh.bat-> 미사용. Spotfire 는 텍스트영역 JS 가 자동 실행
```

## 파일 규칙

`.bat` 은 **ASCII + CRLF** 로만 저장한다. UTF-8 이나 LF 로 저장하면 cmd 가
파싱에 실패해 로그조차 남지 않는다.

python 실행기는 이 순서로 찾는다.

```
HOLDWAITANAL_PYTHON 환경변수  ->  PATH 의 python  ->  py -3
```

`[ERROR] python not found` 가 뜨면 환경변수를 지정한다.

```
setx HOLDWAITANAL_PYTHON "C:\Users\minuk12.choi\AppData\Local\aipforge\python.exe"
```
