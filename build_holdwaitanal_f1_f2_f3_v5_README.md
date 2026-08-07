# 7_holdwaitanal v5 변경사항

## 왜 v4 전체 실행을 중단해야 하는가

v4 full 로그에서 병목은 원천 조회가 아니라 후처리 단계로 확인되었습니다.

- KFR7 StepPath 원천: 6,496,538 rows
- PFR1 StepPath 원천: 3,952,277 rows
- LOT_ID만 선필터 후에도 각각 3,705,257 / 205,062 rows를 DuckDB에 적재
- f1의 m×s join: 74.335초
- TIP wildcard candidate/dedup: 83.561초
- 이후 f1_base / f1_counts / f1_groups에서 다시 대형 행을 집계하므로 장시간 정체

기존 CSV 기반 코드는 이미 전처리된 s가 599,701 rows였기 때문에 f1 전체가 약 26초에 끝났습니다.
따라서 해결책은 f1 SQL 미세조정이 아니라 s/t를 더 일찍 안전하게 축약하는 것입니다.

## v5 핵심 변경 1: StepPath를 현재 ORDER_SEQ 기준으로 DuckDB 적재 전에 축약

원천 SQL의 WHERE 조건은 변경하지 않습니다. getData()로 기존처럼 생테이블을 넓게 조회합니다.

반환된 pandas DataFrame에서 다음 행만 유지합니다.

1. 현재 LOT에 속하고, 현재 ORDER_SEQ 이상이며 STEP_SKIP_YN != 'Y'인 후보 step
2. DE_RANK 계산에 필요한 delay_step_type IN ('S','Y') 행 전체

중요: 현재 ORDER_SEQ 이전 S/Y 행도 보존하므로 기존 `_build_de_rank()` 누적 의미를 유지합니다.

즉 단순히 `order_seq >= current_order`만 걸어 과거 rank 정보를 잃는 최적화가 아닙니다.

## v5 핵심 변경 2: TrackInPrevent는 현재 s에 절대 매칭될 수 없는 규칙을 적재 전에 제거

TrackInPrevent 원천 SQL도 변경하지 않습니다.

Step 전처리를 먼저 완료한 뒤 s에서 wildcard 조합별 match signature를 만듭니다.

TrackInPrevent 규칙의:
- LOT_TYPE
- PROCESS
- STEP
- PPID
- EQPID

wildcard 의미는 그대로 유지됩니다.

현재 파이프라인에서 LOT_TYPE은 '-'이므로 항상 wildcard입니다.
PROCESS/STEP/PPID/EQPID는 각각 '-'인 컬럼만 비교를 면제하고 나머지는 모두 AND 일치해야 합니다.

대형 TrackInPrevent DataFrame을 DuckDB raw 테이블로 통째로 복사하지 않고,
등록된 pandas view와 s signature를 equality/hash join하여 현재 s에 매칭 가능성이 있는 raw row만 물리화합니다.

따라서 결과를 바꾸지 않고 다음 비용을 줄이는 것이 목적입니다.
- 작업 DB 크기
- Tip ttt/window 처리량
- t 최종 행 수
- f1 TIP 후보 비교량

## v5 핵심 변경 3: 처리 순서 변경

기존:
1. lot/equipment/group/tip/step 모두 조회·staging
2. tip
3. step
4. f1

v5:
1. lot/equipment/group 조회
2. dimension 생성
3. StepPath 조회 → 안전 선필터
4. step(s) 완성
5. s 기반 TIP match signature 생성
6. TrackInPrevent 조회 → matchable rule만 staging
7. tip(t) 완성
8. f1 → f2 → f3

## 민감정보 로그

실제 LOT_ID, EQP_ID, process, step 등의 샘플 값은 진단 로그에 남기지 않습니다.
증폭 진단은 다음처럼 count/max만 남깁니다.
- eqp_group_multi_eqp_group_count
- eqp_group_max_eqp_count
- tip_multi_chamber_key_count
- tip_max_chamber_rows_per_key

## 먼저 실행

```powershell
cd D:\PERSONAL_SPACE\SW\python\7_holdwaitanal
python build_holdwaitanal_f1_f2_f3_v5.py --smoke-test
```

통과 후 기존 중단 실행에서 생성된 `output\holdwaitanal_work.duckdb`를 삭제하고:

```powershell
python build_holdwaitanal_f1_f2_f3_v5.py --rebuild
```

## full 실행에서 확인할 핵심 로그

- `[FILTER] KFR7 StepPath needed rows:`
- `[FILTER] PFR1 StepPath needed rows:`
- `[ROWS] s rows=`
- `[FILTER] KFR7 TIP matchable rules:`
- `[FILTER] PFR1 TIP matchable rules:`
- `[ROWS] t rows=`
- `f1-2 m x s join`
- `f1-3 TIP wildcard partitioned join`

기존 CSV 비교 기준:
- old s: 599,701 rows
- old t: 7,672,278 rows
- old f1: 782,812 rows (두 라인 외/시점/원천 차이가 있으므로 절대 행수 동일을 기대하지 않음)

특히 현재 프로젝트 m은 약 4,700 LOT으로 old CSV의 약 9,300 LOT과 모집단 자체가 다르므로 최종 행수 직접 비교보다 `LOT당 step 수`, `f2/f3 비율`, status 분포를 비교하는 것이 맞습니다.
