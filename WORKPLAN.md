# holdwaitanal 작업계획서

**작성일:** 2026-08-10
**목표:** `refer_build_multiwip_f1_f2.py` 의 f1 / f2 / f3 결과를, CSV 경유 없이
Impala → `bigdataquery.getData()` → python 전처리로 직접 산출한다.

---

## 1. 요구사항 재정리 (내가 이해한 것)

| # | 요구 | 해석 |
|---|---|---|
| 1 | `refer_build_multiwip_f1_f2.py` 의 **결과**를 얻는다 | 과정은 자유. f1/f2/f3 테이블의 내용이 같으면 된다 |
| 2 | 기존은 Oracle → Spotfire CSV export → DuckDB 조합 | 입력 4종: `m`(mc_lot) / `s`(step) / `t`(tip) / `h`(hold) |
| 3 | CSV 단계를 없애고 `getData()` 로 직접 적재 | Oracle→Impala 테이블 치환은 `refer_matching_table.txt` 기준 |
| 4 | 로딩·전처리를 최대한 빠르게 | 단, 결과 동일성이 전제 |
| 5 | `build_holdwaitanal_f1_f2_f3_v5.py` 는 **반면교사** | 결과 검증 전에 속도에서 실패. 참조만 하고 답습하지 않는다 |

**사내 환경 제약 (설계의 상수)**

- 쿼리 실행 15분 초과 시 호출 차단 → SQL 에서 결합·연산 금지
- 대부분 테이블에 PK 없음 → `WHERE` 조건은 오히려 느려짐
- 컬럼 축소는 유효한 최적화 → 실제 사용 컬럼만 SELECT

---

## 2. 현재 `get_data.py` 도달 지점

### 완료된 것

| 구성요소 | 상태 | 참조 파이프라인 대응 |
|---|---|---|
| `lot_query` | 완료 (22컬럼, 4,707행) | `m` = `rndplan_mc_lot.csv` |
| `kfr7_tip_query` / `pfr1_tip_query` | 완료 (생테이블 10컬럼) | `t` 원천 |
| `build_tip()` | 완료 (t→ttt→te→tee→es→final) | `t` = `memory_tip.csv` |
| `kfr7_step_path_query` / `pfr1_step_path_query` | 완료 (생테이블 15컬럼) | `s` 원천 |
| `eqp_query` / `eqp_group_query` | 완료 (최신 `impala_insert_time` 만) | `s` 조립용 |
| `build_step()` | 완료 (m→r→p→eqp_group→equipment) | `s` = `rndplan_step.csv` |
| 출력 | Excel / 100만행 초과 시 CSV | — |

### 미착수 — 이번에 만들어야 할 것

| 구성요소 | 내용 |
|---|---|
| **hold 원천** | `MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT` 생테이블 조회 |
| **`build_hold()`** | 기존 `hh → h0 → item_type별 최신건` 로직을 python 으로 |
| **h1 / h2 / h3 분리** | `HOLD LOT`+`FUTUREHOLD` / `EXCEPTION` / `FTkinPvLot` |
| **f1** | m × s × t(wildcard) × h 결합 |
| **f2** | f1 전체 path 유지 summary |
| **f3** | 현스텝 + 현 연속블록만 남긴 최종 summary |
| **출력** | f1/f2/f3 → Parquet / Excel |
| **실행 엔진** | pandas 단독 → **DuckDB 로 전환** |

---

## 3. v5 가 느린 이유 (로그 기반 진단)

`xxx.txt` 실행 로그를 단계별로 분해하면 병목이 명확하다.

| 단계 | 소요 | 판정 |
|---|---|---|
| 원천 조회 + staging | 31s | 정상 |
| StepPath 조회 + 선필터 | 116s | 정상 |
| Step 전처리 | 57s | 정상 |
| TIP 조회 + 선필터 | 129s | 정상 |
| Tip 전처리 | 2s | 정상 |
| f1-2 m×s join | 65s | 정상 |
| f1-3 TIP wildcard join | 39s | 정상 |
| **f1 전체** | **4,282s** | ← **4,178s 가 위 세부단계 밖** |
| **f2 전체** | **705s** | 이상 |
| Parquet 저장 | 371s | 이상 |

### 결론 1 — 병목은 조회도 조인도 아니다

`f1-1 + f1-2 + f1-3 = 103초`. 나머지 **4,178초**는 로그에 세부 타이머가 없는
구간, 즉 조인 이후의 **집계 단계**(`f1_base` / `f1_counts` / `f1_groups`)다.
`STRING_AGG` / `COUNT DISTINCT` / 다중 `GROUP BY` 를 1,400만 행 위에서
여러 번 반복하면서 발생한 것으로 보인다.

### 결론 2 — s 행수가 참조 대비 24배

- 참조 CSV `rndplan_step.csv` = **599,701행**
- v5 재현 `s` = **14,261,295행**

집계 비용이 행수에 비선형으로 붙으므로 이 격차가 4,178초의 직접 원인이다.
다만 **어느 쪽이 옳은지는 아직 확정할 수 없다** (→ 5번 질문 Q1).

### 결론 3 — pandas ↔ DuckDB 왕복

v5 는 getData 결과를 pandas 로 받아 필터한 뒤 DuckDB 테이블로 물리화하고,
다시 pandas 로 꺼내는 구간이 섞여 있다. 1,400만 행 규모에서는 이 복사만으로도
수백 초가 든다. Parquet 저장 371초도 같은 원인일 가능성이 높다.

---

## 4. 개선 설계

### 4-1. 단일 엔진 원칙 — DuckDB

```
getData() → pandas DataFrame → con.register() → 이후 전부 SQL
```

- `register()` 는 zero-copy view 이므로 물리 복사가 없다
- `build_tip()` / `build_step()` 의 pandas 로직도 DuckDB SQL 로 이관
  (윈도우 함수·조인 모두 DuckDB 가 pandas 보다 빠르고 메모리 효율적)
- 중간 산출물은 `CREATE TABLE` 대신 `CREATE VIEW` 를 기본으로, 재사용 2회
  이상인 것만 물리화

### 4-2. 조기 축약 (로딩 직후 1회)

| 원천 | 축약 기준 | 기대 |
|---|---|---|
| StepPath | lot_id ∈ 대상 lot **AND** (order_seq ≥ 현재 order_seq **OR** delay_step_type ∈ S/Y) | de_rank 누적 의미 보존하면서 행 축소 |
| TrackInPrevent | s 의 wildcard signature 에 매칭 가능한 rule 만 | 645만 → 11만 (v5 실측) |
| Equipment / EqpGroup | `impala_insert_time` 최신건만 (SQL 단계) | 이미 적용됨 |

v5 의 이 두 축약 아이디어 자체는 옳다. 유지한다.

### 4-3. TIP wildcard 조인

`(t.PROCESS = '-' OR ms.PROC_ID = t.PROCESS)` 형태의 OR 조건은 non-equi 조인이라
nested loop 으로 떨어진다. v5 처럼 **wildcard mask 별로 분기해 순수 equi-join** 으로
바꾸는 방식을 유지한다 (실측 39초로 이미 빠름).

### 4-4. 집계 단계 재작성 — 여기가 실제 개선 포인트

- 집계 전에 **필요 키만 남긴 좁은 릴레이션**으로 축소 후 `GROUP BY`
- `STRING_AGG` 대상은 사전 `DISTINCT` 로 카디널리티를 줄인 뒤 수행
- 동일 키에 대한 다중 집계는 한 번의 `GROUP BY` 로 통합
- 각 서브단계에 타이머를 심어 병목을 다시 숨기지 않는다

### 4-5. 출력

- 기본은 Parquet (ZSTD, row group 크기 조정)
- Excel 은 f3 만 기본 생성, f1/f2 는 옵션 플래그

---

## 5. 확정이 필요한 사항 (질문)

### Q1. s 행수 기준 — **가장 중요**

참조 CSV `rndplan_step.csv` 는 599,701행인데, 같은 로직을 Impala 원천으로
재현하면 14,261,295행이 나온다. 24배 차이다.

- (a) 참조 CSV 가 Spotfire export 단계에서 잘렸거나 더 좁은 lot 집합이었다
- (b) 참조 CSV 가 맞고, 우리 `build_step()` 이 과다 생성하고 있다

**참조 파이프라인이 마지막으로 산출한 f1 / f2 / f3 의 실제 행수**를 알려주시면
어느 쪽인지 즉시 판별됩니다. (b) 라면 로직 버그를 잡아야 하고,
(a) 라면 1,400만 행을 전제로 성능 설계를 해야 합니다.

### Q2. 최종 산출물 범위

f1 / f2 / f3 중 실제로 필요한 것은 무엇입니까? f1 은 원본 결합 결과라
용량이 가장 크고 Parquet 저장에만 371초가 걸렸습니다. 검증용으로만 필요하다면
기본 미저장 + 옵션으로 두겠습니다.

### Q3. hold 의 `status_seq` 필터

기존 Oracle 쿼리는 `status_seq <> '2'` (조치완료 제외)였습니다.
새로 주신 생테이블 쿼리에는 이 조건이 없습니다.

- 기존대로 `'2'` 제외를 python 단에서 적용할까요?
- `'3'`(조치불가)도 제외 대상인지 함께 확정 부탁드립니다.

### Q4. hold 의 대상 라인

기존 쿼리는 `line_id IN ('PFR1','KFR7')` 인데, 새 지시는 `('KFR4','KFR7','PFR1')`
로 KFR4 가 추가됐습니다. 그런데 `lot_query` 의 대상 라인에는 KFR4 가 없어
결합 시 버려집니다.

- KFR4 를 지금 넣는 것은 향후 확장 대비인가요?
- 아니면 lot 쪽에도 KFR4 를 추가해야 합니까?

### Q5. 참조 파이프라인의 결손 컬럼을 채울지

참조 코드의 `TODO_ITEMS` 에 따르면 `s` CSV 에 `AREA` 와
`EQP_STATUS_CHANGE_TIME` 이 없어 해당 값이 null 로 나갔습니다.
지금 구조에서는 둘 다 채울 수 있습니다.

- 채우면 **참조 결과와 값이 달라집니다** (null → 실값)
- "결과가 같아야 한다"를 엄격히 적용해 null 을 유지할지,
  개선으로 보고 채울지 확정 부탁드립니다.

### Q6. 목표 실행시간과 실행 주기

- 배치인가요, 수동 실행인가요?
- 전체 몇 분 이내를 합격으로 보십니까? (원천 조회만으로 이미 약 5분입니다)

---

## 6. 착수 순서 (Q1 회신 후)

| 순서 | 작업 | 선행조건 |
|---|---|---|
| 1 | hold 원천 쿼리 + `build_hold()` + h1/h2/h3 | Q3, Q4 |
| 2 | DuckDB 엔진 전환 (tip/step 로직 SQL 이관) | — |
| 3 | f1 구현 (m×s, TIP wildcard, h 결합) | Q1, Q5 |
| 4 | f2 / f3 구현 | 3번 |
| 5 | 참조 결과와 행수·컬럼 대조 검증 | Q1 |
| 6 | 단계별 타이머 기반 성능 튜닝 | Q6 |
| 7 | 출력 (Parquet / Excel) | Q2 |

2번은 Q1 회신과 무관하게 착수 가능하므로, 회신 대기 중에 먼저 진행할 수 있습니다.

---

# 부록 A — v5 전체 로그 분석 (2026-08-10 추가)

## A-1. 확보된 행수

| 단계 | 행수 |
|---|---|
| lot (m) | 4,707 |
| step candidate (eqp_group 결합 전) | 3,498,494 |
| s (eqp_group 결합 후) | 14,261,295 |
| t | 118,808 |
| **f1** | **18,024,925** |
| **f2** | **3,748,947** |
| **f3** | **8,271** |

## A-2. 핵심 발견 1 — 만든 행의 79%를 다시 버린다

```
f1 18,024,925 → f2 3,748,947   (79.2% = 14,275,978행이 DISTINCT 로 소멸)
f2  3,748,947 → f3     8,271   (99.78% 소멸)
```

`f2_duplicate_rows_removed = 14,275,978`. 그리고 그 소멸량은
eqp_group 증폭량(`step_eqp_group_amplification = 4.08`)과 거의 일치한다.

**원인:** `s` 를 만들 때 설비그룹의 EQP 를 행으로 펼쳐 4.08배로 부풀린 뒤,
f2 에서 `eqpgroup` / `eqpgroup_cham` 문자열로 다시 합치면서 그 행들이
전부 중복이 되어 사라진다. 즉 **펼쳤다가 도로 합치는 왕복**이다.

**개선:** eqp_group 을 `eqp_group_name → EQP 목록 문자열` 로 **미리 집계한
dimension** 으로 만들어 붙인다. 행 확장이 아예 발생하지 않는다.

- `s` : 14,261,295 → 3,498,494 (4.08배 감소)
- `f1`: 18,024,925 → 약 4,400,000
- f2 의 `STRING_AGG` / `DISTINCT` 단계 소멸 (704초 구간)

단, 아래 두 곳은 EQP 단위 값이 필요하므로 별도 소형 조인으로 처리한다.

- TIP 정확매칭의 `EQPID` 비교 → 로그상 `tip_exact_rule_rows = 0` 이라
  현재 데이터에서는 정확매칭이 한 건도 없음. wildcard 만 존재
- `down` (설비 상태) → eqp_group 단위 사전 집계로 대체 가능

## A-3. 핵심 발견 2 — `step_skip_yn` NULL 처리 버그

Oracle 원본:

```sql
WHERE sp.step_skip_yn <> 'Y'
```

Oracle 에서 `NULL <> 'Y'` 는 TRUE 가 아니라 **UNKNOWN** 이므로 해당 행은
**제외**된다. 그런데 두 재현 구현 모두 NULL 을 **포함**하고 있다.

| 구현 | 코드 | NULL 행 |
|---|---|---|
| Oracle 원본 | `step_skip_yn <> 'Y'` | 제외 |
| `get_data.py:526` | `path['step_skip_yn'].ne('Y')` | **포함** |
| `v5:1143` | `COALESCE(CAST(step_skip_yn AS VARCHAR),'') <> 'Y'` | **포함** |

`step_skip_yn` 이 NULL 인 비율이 높다면 이것만으로 candidate 행이 크게 부풀 수
있다. 참조 CSV 기준 lot 당 잔여 step 은 31개, v5 재현은 743개다.

**검증 방법** (getData 결과에 대해 1줄):

```python
print(df_kfr7_path['step_skip_yn'].isna().mean(),
      df_kfr7_path['step_skip_yn'].value_counts(dropna=False))
```

NULL 비율이 90% 를 넘으면 이것이 24배 격차의 주원인이다.

## A-4. 핵심 발견 3 — f3 는 전체 경로가 필요 없다

`f3_rows = 8,271`, `f3_lot_distinct = 3,591` → lot 당 2.3행.

f3 조건은 `현스텝 = '현스텝' OR de_rank = current_de_rank` 뿐이며, 이 판정에
필요한 값(`lot_id`, `order_seq`, `de_rank`)은 **eqp_group 결합 전 candidate
단계에서 이미 확정된다**.

따라서 f3 만 필요하다면 3,498,494행 중 약 3,600행만 남기고 나머지 확장·조인을
전부 생략할 수 있다. 이 경우 전체 파이프라인은 **원천 조회 시간(약 5분)이
지배**하고 후처리는 수 초 수준이 된다.

f2(전체 path 유지본)가 산출물로 필요한지 여부가 설계를 가르는 분기점이다.

## A-5. 수정된 병목 판정

| 구간 | 소요 | 재판정 |
|---|---|---|
| f1 집계 (세부타이머 밖) | 4,178s | eqp_group 4배 확장 + 대형 GROUP BY |
| f2 생성 | 705s | 확장분을 STRING_AGG 로 되접는 비용 |
| Parquet 저장 | 371s | 18M행 물리화 |

세 구간 모두 **A-2 의 왕복 제거**로 동시에 해소된다.

## A-6. 갱신된 질문

기존 Q1(참조 f1/f2/f3 행수)은 로그가 없어 확인 불가로 종결한다.
대신 아래 2건이 착수 전 필수 확정 사항이다.

### Q1'. f1 / f2 가 산출물로 필요한가?

- **f3 만 필요** → A-4 적용. 후처리 수 초. 가장 빠름
- **f2 도 필요** → A-2 적용. 후처리 수십 초 목표
- **f1 원본도 필요** → f1 은 검증용으로만 두고 기본 미저장 권장

### Q2'. `step_skip_yn` NULL 행을 포함할 것인가?

- Oracle 원본 동작(제외)에 맞출지
- 현재 재현 동작(포함)을 유지할지

A-3 의 검증 코드 결과를 함께 알려주시면 판단이 빨라진다.
