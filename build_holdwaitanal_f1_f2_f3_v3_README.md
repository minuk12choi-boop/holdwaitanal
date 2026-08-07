# holdwaitanal f1/f2/f3 v3

## 변경 이유
v2의 f1 TIP 매칭은 PROCESS/STEP/PPID/EQPID/LOT_TYPE wildcard를 여러 OR 조건으로 한 번에 조인했다.
수백만 행 TIP에서 이 방식은 equality hash join으로 최적화되기 어렵고 f1에서 장시간 정체될 수 있다.

## v3 핵심 변경
- wildcard 업무 규칙은 그대로 유지한다.
- `-`, blank, NULL인 컬럼만 wildcard다.
- 나머지 컬럼은 모두 AND 일치해야 한다.
- LOT_TYPE/PROCESS/STEP/PPID/EQPID 5개를 wildcard mask(0~31)로 분류한다.
- mask별로 wildcard 컬럼을 JOIN 조건에서 제거하고, 나머지 컬럼만 equality join한다.
- 따라서 OR-heavy join을 최대 32개의 hash-friendly equality join으로 분해한다.
- 동일 semantic TIP 결과는 기존처럼 ms_row_id+EQPCHAM+PREVENT+EQPISSUE+TIP_EVENTTIME+EQPISSUETIME 기준으로 1건을 유지한다.
- core PROCESS/STEP/PPID/EQPID가 모두 exact인 규칙은 기존 exact 우선순위를 유지한다.

## 먼저 실행
현재 v2 전체 실행이 f1에서 장시간 멈췄다면 중단 후 아래를 실행한다.

```powershell
cd D:\PERSONAL_SPACE\SW\python\7_holdwaitanal
python build_holdwaitanal_f1_f2_f3_v3.py --smoke-test
```

확인할 로그:

```text
[TIMER] f1-1 m_base ...
[TIMER] f1-2 m x s join ...
[TIMER] f1-3 TIP wildcard partitioned join ...
[F1][TIP MATCH] wildcard_mask=.., rules=...
```

민감 데이터 값은 출력하지 않고 mask별 규칙 수만 출력한다.

## 전체 실행
스모크 테스트 통과 후:

```powershell
python build_holdwaitanal_f1_f2_f3_v3.py --rebuild
```

## 결과 비교 시 필요한 안전한 숫자
- tip_wildcard_mask_rule_counts
- tip_wildcard_rule_rows
- tip_exact_rule_rows
- tip_match_candidate_rows
- tip_match_candidate_amplification_vs_ms
- tip_match_rows_after_semantic_dedup
- tip_match_semantic_duplicates_removed
- f1_rows
- f2_rows
- f3_rows

실제 LOT_ID/EQP_ID/PPID 값은 공유할 필요가 없다.
