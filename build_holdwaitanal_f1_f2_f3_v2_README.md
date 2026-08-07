# 7_holdwaitanal 통합 파이프라인 v2

## 권장 실행 순서

### 1. 스모크 테스트

```powershell
cd D:\PERSONAL_SPACE\SW\python\7_holdwaitanal
python build_holdwaitanal_f1_f2_f3_v2.py --smoke-test
```

스모크 테스트는 다음 원칙으로 동작합니다.

- Lot: 전체 조회(약 4천 행)
- Equipment: 전체 조회(약 3.7만 행)
- EqpGroup: 전체 조회(약 1.7만 행)
- TrackInPrevent: 라인별 최대 5,000행
- StepPath: 실제 Lot에서 라인별 최대 20개 LOT_ID를 골라 최대 20,000행
- `f1 -> f2 -> f3` 생성과 검증까지 실제로 수행
- 본 실행 Parquet/Excel은 생성하지 않음
- 별도 작업 DB `output/holdwaitanal_smoke.duckdb` 사용

정상 종료 시 다음 메시지가 나옵니다.

```text
[SMOKE PASS] f1/f2/f3 생성 및 검증 단계까지 완료
```

### 2. 본 실행

```powershell
python build_holdwaitanal_f1_f2_f3_v2.py --rebuild
```

Excel 검증본도 필요한 경우:

```powershell
python build_holdwaitanal_f1_f2_f3_v2.py --rebuild --export-excel
```

## v2 변경점

1. `getData()` 반환형을 pandas DataFrame으로 고정
   - 사전 테스트에서 확인된 현재 환경을 기준으로 함.
   - 예상과 다른 타입이 반환되면 대용량 묵시적 변환을 하지 않고 즉시 오류 처리.

2. 대형 DataFrame 복사 제거
   - 컬럼 정규화 과정의 `.copy()` 제거.
   - 조회 DataFrame의 컬럼명만 in-place로 정규화한 뒤 DuckDB에 적재.

3. 명시적 메모리 해제
   - 각 원천 staging 직후 `del df` 및 `gc.collect()` 실행.

4. 스모크 테스트 작업 DB 분리
   - 본 실행: `output/holdwaitanal_work.duckdb`
   - 스모크: `output/holdwaitanal_smoke.duckdb`

5. TrackInPrevent wildcard 규칙 유지
   - LOT_TYPE, PROCESS, STEP, PPID, EQPID 각각 `-`이면 해당 컬럼만 wildcard.
   - 다른 조건은 모두 AND 조건으로 반드시 일치해야 함.

6. AREA 및 h 관련 값
   - AREA는 NULL 유지.
   - h 원천은 이번 버전에서 제외하고 hold/exception/ftp 관련 최종 컬럼은 NULL 유지.
