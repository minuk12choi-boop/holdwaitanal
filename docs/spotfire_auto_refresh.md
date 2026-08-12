# Spotfire 자동 새로고침 방안

## 문제

```
Spotfire Analyst 를 열어야만  →  쿼리 재조회  →  data function 실행  →  S3 업로드
```

Spotfire 를 안 열면 S3 파일이 고정된다. 15분 주기 갱신을 원하는데 사람이 매번
열 수는 없다.

추가로 클라우드(웹) Spotfire 는 서버 Python 을 쓰므로 로컬에 설치한 boto3 가
잡히지 않는다. **Analyst(설치형)에서만** 업로드가 동작한다.

---

## 주기 설계 (30분)

datalake 2시간 적재 제약이 없어졌으므로 30분 주기로 맞춘다.
남은 제약은 **Spotfire 로딩 + S3 업로드 약 10분** 뿐이다.

```
:00  Spotfire 새로고침 시작
:10  S3 업로드 완료 (_manifest.json 이 마지막에 기록됨)
:15  build_f3 실행  ->  DB 적재  ->  웹 반영
:30  다음 회차
```

### 엇갈림을 막는 두 가지 장치

**① 완결 표시 (_manifest.json)**

업로드는 8개를 순차로 올린다. 중간에 build_f3 가 읽으면 서로 다른 시점의
파일이 섞인다. 그래서 Spotfire 가 **8개를 다 올린 뒤 마지막에**
`_manifest.json` 을 쓴다. 이 파일이 있으면 그 회차는 완결된 것이다.

**② 중복 처리 방지**

build_f3 는 매니페스트의 `finished_at` 을 직전 처리분과 비교한다.
같으면 아직 새로 안 올라온 것이므로 즉시 종료한다.

```
[SKIP] 이미 처리한 회차입니다 (finished_at=2026-08-12 05:00:00).
```

덕분에 **스케줄러 시각을 정밀하게 맞출 필요가 없다.** build_f3 를 15분마다
돌려도 새 회차가 있을 때만 실제로 일한다. Spotfire 가 늦어져도 다음 실행에서
자연스럽게 따라잡는다.

무시하고 강제 실행하려면 `--force` 를 준다.

### 데이터 크기 대응 — 로컬 캐시 (ETag 기준)

30분마다 1.8 GB 를 내려받으면 86 GB/일이다.

**LastModified 로 판별하면 안 된다.** Spotfire 가 30분마다 8개를 전부
재업로드하므로 내용이 같아도 시각은 매번 바뀐다. 그러면 캐시가 무력화되어
결국 매번 다 받게 된다.

그래서 **ETag(업로드 내용의 MD5)** 를 쓴다.

```
head_object  ->  ETag / ContentLength 만 받아 비교   (본문 미전송)
  같으면  ->  로컬 캐시 사용, get_object 아예 호출 안 함
  다르면  ->  get_object 로 본문 수신 후 캐시 갱신
```

`pandas.to_pickle` 은 같은 DataFrame 에 대해 바이트가 재현되므로,
**내용이 안 바뀌었으면 재업로드해도 ETag 가 같다.** 검증 완료.

| 상황 | 본문 전송 |
|---|---|
| 최초 | O |
| 내용 동일한 재업로드 | **X** (캐시) |
| 내용 변경 | O |

STEP_PATH / TIP 은 공정 경로·규칙이라 실제로 바뀌는 빈도가 낮다. 대부분의
회차에서 1.7 GB 전송이 사라진다.

캐시는 `getdata/s3cache/` 에 쌓이고 git 에는 올라가지 않는다.
끄려면 `s3_source.USE_CACHE = False`.

## 방안 A. Spotfire 를 상주시키고 자동 새로고침 (권장)

Analyst 를 하루 종일 켜두고, 분석 파일이 스스로 새로고침하게 한다.
**프로세스를 반복 기동하지 않아 가장 안정적이다.**

### A-1. 데이터 테이블 자동 새로고침

Spotfire 에서 각 데이터 테이블에 대해:

```
Data > Data Table Properties > (테이블 선택) > Source Information
  Load method : Linked to source     (Imported 면 자동 갱신이 안 된다)
```

`Linked to source` 로 두면 Spotfire 가 주기적으로 원본을 다시 읽는다.
주기 설정이 UI 에 없으면 A-2 를 쓴다.

### A-2. IronPython + HTML 타이머 (주기를 직접 정할 때)

Spotfire 에는 네이티브 타이머가 없다. 텍스트 영역의 JavaScript 로 버튼을
주기적으로 눌러 IronPython 스크립트를 실행시키는 방식이 널리 쓰인다.

**① IronPython 스크립트 등록** (Tools > Register Scripts, 이름 `RefreshFast`)

```python
# 빠른 테이블만 새로고침
for name in ["PFR1_KFR7_LOT", "PFR1_KFR7_HOLD", "PFR1_KFR7_EQUIPMENT",
             "PFR1_KFR7_EQP_GROUP", "PFR1_KFR7_MATERIALWORKSTATUS"]:
    if Document.Data.Tables.Contains(name):
        Document.Data.Tables[name].Refresh()
```

느린 테이블용으로 `RefreshSlow` 도 같은 방식으로 하나 더 만든다.

```python
for name in ["PFR1_KFR7_STEP_PATH", "PFR1_KFR7_TIP"]:
    if Document.Data.Tables.Contains(name):
        Document.Data.Tables[name].Refresh()
```

**② 텍스트 영역에 버튼 + 타이머**

텍스트 영역을 하나 만들고 `RefreshFast` 를 실행하는 Action Control(버튼)을
넣는다. 버튼 이름을 `btnFast` 로 지정한 뒤, 같은 텍스트 영역의 HTML 편집에서
아래를 추가한다.

```html
<span id="tick" style="color:#6B7280;font-size:11px"></span>
<script>
  // 15분마다 버튼을 눌러 데이터 테이블을 새로고침한다.
  // data function 의 Refresh 를 Automatic 으로 두면 업로드까지 연쇄된다.
  setInterval(function () {
    var b = document.getElementById("btnFast");
    if (b) { b.click(); }
    document.getElementById("tick").innerText =
      "last: " + new Date().toLocaleTimeString();
  }, 15 * 60 * 1000);
</script>
```

**③ data function 설정**

```
Tools > Register Data Functions > (s3drive) > Refresh Function : Automatic
```

입력 테이블이 갱신되면 스크립트가 자동 실행되어 S3 로 올라간다.

### A-3. PC 가 잠들지 않게

```
제어판 > 전원 옵션 > 절전 : 안 함
화면 보호기 : 없음 (또는 잠금 시에도 스크립트는 계속 돈다)
```

Windows 절전에 들어가면 Spotfire 도 멈춘다.

---

## 방안 B. 작업 스케줄러가 열고 닫기

15분마다 Spotfire 를 새로 띄우고, 업로드가 끝나면 스스로 종료시킨다.

**단점이 크다.** Spotfire 기동에만 30초~1분이 걸리고, 라이선스 체크·로그인
세션 문제가 생길 수 있으며, 뜨는 창이 화면을 계속 가로챈다.
**A 가 가능하면 A 를 쓴다.**

### 구현

**① 분석 파일에 종료 스크립트 등록**

data function 실행 후 문서를 닫는 IronPython 을 마지막에 태운다.

```python
from Spotfire.Dxp.Application import Application
Application.Current.Close()
```

**② 배치 파일** (`scripts/run_spotfire_refresh.bat`)

```bat
@echo off
setlocal
set DXP="C:\Program Files (x86)\TIBCO\Spotfire\12.0.4\Spotfire.Dxp.exe"
set FILE="D:\PERSONAL_SPACE\SW\python\7_holdwaitanal\MFM.dxp"
start "" /wait %DXP% %FILE%
endlocal
```

**③ 작업 스케줄러**

- 트리거: 매일 00:00 시작, **15분 간격** 반복, 기간 1일
- 설정: `새 인스턴스 시작 안 함` (겹침 방지)
- **사용자가 로그온할 때만 실행** (GUI 앱이라 세션이 필요하다)

---

## 방안 C. Spotfire 를 빼고 Oracle 에 직접 접속 (근본 해결)

파이썬이 Oracle 에 직접 붙으면 이 문제 전체가 사라진다.

```
python (oracledb) → Oracle → 전처리 → DB → 웹
```

- Spotfire 상주 불필요, GUI 불필요, 15분이든 5분이든 자유
- S3 왕복 1,803 MB 가 통째로 사라진다
- `reference/raw_of_raw_table.txt` 의 Oracle 쿼리를 그대로 쓴다
- `build_f3.py` 는 `SOURCE = "oracle"` 하나 추가하면 된다

**필요한 것**

```
pip install oracledb
```

접속 정보(host / port / service_name 또는 TNS, 계정) 와 방화벽 허용.

**확인 부탁** — 사내에서 개인 PC 가 Oracle 에 직접 접속할 권한을 받을 수
있는지가 관건이다. Spotfire 가 접속하고 있다는 건 네트워크 경로 자체는
열려 있다는 뜻이므로, 계정 발급만 되면 가능성이 있다.

---

## 정리

| 방안 | 난이도 | 안정성 | 권장 |
|---|---|---|---|
| A. Spotfire 상주 + 자동 새로고침 | 중 | 상 | **지금 바로는 이것** |
| B. 스케줄러가 열고 닫기 | 중 | 하 | A 가 안 될 때만 |
| C. Oracle 직접 접속 | 낮음(권한만 되면) | 최상 | **가능하면 이것** |

### 함께 조정할 것

A 든 C 든, **build_f3 주기도 같이 당겨야** 15분 갱신이 의미를 갖는다.
다만 STEP_PATH / TIP 1.7 GB 를 15분마다 내려받는 것은 비현실적이므로,

- 빠른 테이블(22 MB)만 15분 주기로 갱신
- STEP_PATH / TIP 은 하루 2~4회
- `build_f3.py` 에 S3 파일 로컬 캐시를 두어 변경된 것만 내려받기

이 세 가지를 함께 적용해야 한다. 캐시는 요청 주시면 구현한다.
