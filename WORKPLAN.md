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
