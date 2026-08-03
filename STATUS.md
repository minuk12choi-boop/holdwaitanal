# 진행 현황 (STATUS)

**최종 갱신:** 2026-08-03
**갱신자:** Claude Code (로컬 작업 세션)
**현재 단계:** 착수 전 — 설계서 검토 및 데이터 자산 분석 완료

---

## 1. 지금까지 한 일

- [x] 저장소 클론 및 기존 자산(`x.txt`, `xx.txt`, `xx_impala.py`) 분석
- [x] 화면 설계서(`docs/blueprint_fab_flow_monitor.md`) 전체 검토
- [x] 설계서 요구사항 ↔ 실제 보유 데이터 대조
- [x] 소통 채널 구조(`WORK/ORDERS`, `WORK/REPORTS`) 구성
- [ ] 구현 착수 — **미착수. 아래 4번 결정 대기 중**

---

## 2. 남은 일 (설계서 §24 기준)

| Phase | 내용 | 상태 | 비고 |
|---|---|---|---|
| 1 | 전역 필터 + FlowStack 5패널 + 공통 시간축 + 상태 연동 | 미착수 | Mock 데이터로 즉시 착수 가능 |
| 2 | Flow Loss / WIP / Hold / Wait 기본 Drill-down | 미착수 | Mock 필요 |
| 3 | Lot Balance (Layer 통합 / Proc별) | 미착수 | **Layer 원천 미확정** |
| 4 | 이상 자동 표시, 진행제약 누적시간, 순 진행불가시간, 정상 범위, URL 상태 | 미착수 | 산식 미정의 |

---

## 3. 다음에 해야 할 일

1. 아래 **4번 결정 대기 항목** 회신 받기
2. 기술 스택 확정 후 프로젝트 초기화 (Vite + React + TypeScript + ECharts + Zustand 제안)
3. **데이터 계약(TypeScript 인터페이스) 먼저 확정** — 산식 없이도 화면이 요구하는 데이터 모양은 지금 고정 가능. 이후 실제 쿼리를 그 자리에 끼워 넣는 방식.
4. Phase 1 구현 (Mock 데이터)

---

## 4. 대기 중인 결정 — **회신 필요**

### 4-1. Line 개수 불일치 (중요)

- 설계서: **4개 Line** (Line A / B / C / D), 색상 파랑·초록·주황·앰버 고정
- 실제 쿼리(`xx.txt`, `xx_impala.py`): **3개 Line** — `KFR4`, `PFR1`, `KFR7`

→ 네 번째 Line이 무엇인지, 아니면 3개로 축소할지 확정 필요.
→ 실제 Line 코드를 그대로 화면에 쓸지, A/B/C/D로 별칭 처리할지도 함께 결정 필요.

### 4-2. Lot Type 정의 불일치

- 설계서: `전체` / `PP` / `PG(TT)` 3종
- 실제 쿼리 필터: `lot_type IN ('PP', 'PB', 'PG', 'TT')` — **4종이며 `PB`가 설계서에 없음**

→ `PG`와 `TT`를 한 그룹으로 묶는 것이 맞는지, `PB`는 어디에 속하는지 확정 필요.

### 4-3. Layer 원천 (Lot Balance 전제 조건)

Lot Balance(설계서 §11)는 X축이 Layer인데, 현재 스냅샷 소스인 `mc_lot`(a/b/c)에는 **layer 컬럼이 없다.**
Layer는 다음 두 곳에만 존재한다.

- `d` = `FAB.M_LOT_TRANSN_HIST` → `layer_id`
- `i` = `SMICDC_NRD_STEPRULE` → `layerid`

→ 어느 쪽을 정본으로 쓸지, `step_seq` → `layer` 매핑 테이블이 별도로 있는지 확정 필요.
→ 확정 전까지 Phase 3은 착수 불가.

### 4-4. 설계서 자체의 모호/충돌 (5건)

아래는 제 권장안입니다. 이대로 진행할지 회신 주시면 그대로 구현합니다.

| # | 항목 | 권장안 |
|---|---|---|
| 1 | §6.4 `상세 비교`가 Lot Type의 4번째 값인지 별도 토글인지 | 3-way 세그먼트(전체/PP/PG) + 독립 토글로 분리 (§21 `lotTypeDetailMode`와 정합) |
| 2 | §6.2 Line 체크박스 vs §7.6 Line 선 클릭의 구분 | 체크박스 = 표시 여부(`selectedLines`), 선 클릭 = 분석 대상(`focusedLine`). 선 클릭 시 해당 Line 강조 + 나머지 흐리게 |
| 3 | §6.6 `기준일=100`을 HOLD율·WAIT율에 적용하면 의미 왜곡 | 비교 모드는 MOVE / WIP TURN / 재공에만 적용, 비율 지표는 절대값 고정 |
| 4 | §4.2 세로 스크롤 허용 vs §7.8 5패널 크로스헤어 동기화 충돌 | FlowStack은 뷰포트 높이에 5패널 고정(§7.3 상대높이로 배분), Cause Explorer만 내부 스크롤 |
| 5 | §11.1 Lot Balance 진입 시 Line 미선택 처리 | 첫 번째 활성 Line 자동 적용 + 헤더에 선택 Line 명시 |

### 4-5. 설계서에 누락된 항목

- **이상(anomaly) 판정 기준** — §7.4 이상 배지, §17.6 이상 지점 클릭, §24 Phase 4 "이상 구간 자동 표시"가 모두 이 기준에 의존하는데 정의가 없음
- 로딩 / 에러 / 데이터 없음 상태의 화면 표현 (§7.9 결측 규칙만 존재)
- 상세 목록 CSV export 여부 (상세 테이블이 5개 화면에 등장)
- 데이터 갱신 주기 / 수동 새로고침 유무

---

## 5. 우려되는 일

### 5-1. WAIT성 진행불가율 — 원천 데이터가 전혀 없음 (최대 리스크)

설계서 §13~§16이 요구하는 원천 중 **보유 테이블 9개 어디에도 다음이 없다.**

| 요구 항목 | 원천 테이블 | 상태 |
|---|---|---|
| 설비 Status (Down / PM / Local) | — | **없음** |
| TIP | — | **없음** |
| Exception | 후보: `mc_lot.abnrml_type`, `TODOPLAN.abnormal`, `TODOPLAN.einsteptype/einno` | 추정 단계 |
| FTP | 후보: `STEPRULE.ruletype / ruleaction / category` | 추정 단계 |

→ 설비 Status 이력 테이블이 추가로 필요합니다. 이것 없이는 FlowStack 5번째 패널과 Cause Explorer의 Wait Block Analysis 전체가 Mock으로만 존재하게 됩니다.
→ 설계서 §25-24("임의 추정 금지")에 따라 Exception/FTP 후보 컬럼도 확인 전까지 사용하지 않겠습니다.

### 5-2. 시계열 데이터 부재 — 현재 쿼리는 스냅샷 전용

`xx.txt` / `xx_impala.py`는 **현재 시점 1회 스냅샷**입니다. 설계서는 최근 30일 시계열(§6.1)을 기본으로 합니다.

| 지표 | 현재값 | 시계열 |
|---|---|---|
| 재공 | 산출 가능 (`mc_lot`) | **불가** |
| HOLD율 | 산출 가능 (`lot_status_seg='Hold'`) | **불가** |
| MOVE | 불가 | `M_LOT_TRANSN_HIST`로 가능성 있음 (MOVE 판정 이벤트 정의 필요) |
| WIP TURN | 불가 | 재공 시계열에 의존 → **불가** |
| WAIT성 진행불가율 | 불가 | **불가** |

→ 일별 재공 스냅샷 테이블이 별도로 존재하는지, 아니면 이력에서 재구성해야 하는지 확인이 필요합니다.

### 5-3. HOLD 파트원 분석의 개인정보

설계서 §12.4는 등록자 실명·소속을 표 형태로 노출합니다. `user_id` / `inuserid` / `etc_opertr_id` 가 후보 컬럼입니다.
→ 실명 노출 정책(마스킹 여부, 권한별 표시)을 사전에 정해두는 편이 안전합니다.

### 5-4. Oracle → Impala 변환본 검토 결과

`xx_impala.py`는 전반적으로 정확합니다. 다음 2건만 확인 권장.

1. **`lot_level` / `order_seq` / `prirt_no` 타입 변환** — 원본은 `TO_CHAR(DOUBLE)`, 변환본은 `CAST(CAST(x AS BIGINT) AS STRING)`. 값이 정수면 무해하나, 소수부가 존재하면 절삭됩니다.
2. **`MI_LOT_TRANSN_HIST_V`(뷰) → `FAB.M_LOT_TRANSN_HIST`(테이블)** — 뷰에 내장된 필터 조건이 있었다면 결과가 달라질 수 있습니다.

그 외 `(+)` outer join → LEFT JOIN 변환, `c` CTE의 `distinct + max() over` → `GROUP BY` 치환, 날짜 포맷(`yyyymmdd hh24:mi:ss` → `yyyyMMdd HH:mm:ss`, 24시간제 유지)은 모두 정확합니다.

---

## 6. 에러 / 막힌 지점

현재 없음. (아직 코드 실행 단계가 아님)

- Impala 접속 환경이 로컬에 없으므로 `xx_impala.py`의 실제 실행 검증은 하지 못했습니다. 구문 검토만 수행했습니다.

---

## 7. 요약

**지금 당장 막는 것:** 4-1(Line 개수), 4-2(Lot Type), 4-4(설계서 모호 5건)
**Phase 3을 막는 것:** 4-3(Layer 원천)
**프로젝트 전체의 최대 리스크:** 5-1(WAIT 원천 부재), 5-2(시계열 부재)

4-4는 제 권장안대로 진행해도 되는지만 알려주시면 됩니다.
4-1·4-2만 확정되면 나머지는 Mock으로 두고 **Phase 1 착수 가능합니다.**
