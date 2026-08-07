# build_holdwaitanal_f1_f2_f3_v4 변경사항

## 현재 판단

- v3 smoke test PASS
- f1 전체 2.914초, TIP wildcard partitioned join 0.053초로 기존 수십 분 병목은 해결됨
- Step × EqpGroup 3.637배 증폭은 EQP group 내 실제 EQP 후보 확장이라는 업무 요구와 일치
- TIP wildcard는 LOT_TYPE/PROCESS/STEP/PPID/EQPID 각 컬럼별 독립 wildcard이며, 나머지 비-wildcard 조건은 모두 AND equality 유지

## v4 추가 최적화

### StepPath 적재량 축소
원천 SQL에는 추가 WHERE를 넣지 않습니다. 기존 의도대로 전체 StepPath를 `getData()`로 조회합니다.

단, pandas DataFrame 반환 직후 현재 `lot_query`에 존재하는 동일 LINE의 LOT_ID만 남기고 DuckDB에 적재합니다. 기존 build_step도 결국 current_lot과 INNER JOIN하여 나머지 LOT을 버렸으므로 최종 결과는 동일해야 합니다.

로그에 다음이 추가됩니다.

- `KFR7_step_local_filter_before`
- `KFR7_step_local_filter_after`
- `KFR7_step_local_filter_removed`
- PFR1 동일 항목

### DuckDB 작업 테이블 수명 단축
각 단계 완료 후 최종 결과에 더 이상 필요하지 않은 raw/intermediate table을 즉시 DROP합니다.

이 변경은 Windows에서 보이는 `.duckdb` 파일 크기를 즉시 축소한다고 보장하지는 않지만, 내부 공간 재사용 및 이후 연산 대상 감소에 도움이 됩니다.

## 실행 순서

기존 v2/v3 full 실행에서 생성된 `output/holdwaitanal_work.duckdb`가 있다면 실행을 종료한 뒤 삭제하고 새로 시작하는 것을 권장합니다.

### 1. smoke test

```powershell
cd D:\PERSONAL_SPACE\SW\python\7_holdwaitanal
python build_holdwaitanal_f1_f2_f3_v4.py --smoke-test
```

### 2. full rebuild

```powershell
python build_holdwaitanal_f1_f2_f3_v4.py --rebuild
```

## 확인할 핵심 로그

- `f1-3 TIP wildcard partitioned join` 실행시간
- `KFR7/PFR1_step_local_filter_before/after/removed`
- `tip_match_candidate_rows`
- `tip_match_semantic_duplicates_removed`
- `f1/f2/f3_rows`

민감한 LOT_ID/EQP_ID 샘플은 전달할 필요가 없습니다.
