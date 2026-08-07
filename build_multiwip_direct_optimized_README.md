# MultiWIP 직접 조회 통합 코드 사용 안내

## 생성 파일

- `build_multiwip_direct_optimized.py`: DB 조회부터 f1/f2/f3 생성까지 한 번에 수행하는 통합 코드

## 기본 실행

```powershell
cd D:\PERSONAL_SPACE\SW\python\5_multiwip
python build_multiwip_direct_optimized.py --rebuild
```

검증용 f2/f3 Excel도 필요한 경우:

```powershell
python build_multiwip_direct_optimized.py --rebuild --export-excel
```

## 설치 패키지

DuckDB는 별도 프로그램이나 서버가 아니라 Python 패키지입니다.

```powershell
pip install duckdb pandas pyarrow openpyxl
```

## 출력 구조

```text
output/
├─ multiwip_work.duckdb
├─ duckdb_temp/
├─ parquet/
│  ├─ f1_result.parquet
│  ├─ f2_result.parquet
│  └─ f3_result.parquet
├─ excel/
│  ├─ f2_result.xlsx
│  └─ f3_result.xlsx
└─ log/
   └─ build_multiwip_direct_YYYYMMDD_HHMMSS.md
```

## 이번 버전의 명시적 전제

- `h` 원천은 제외했습니다.
- `hold`, `exception`, `ftp` 관련 최종 컬럼은 유지하되 `NULL`로 출력합니다.
- `AREA`는 `NULL`로 출력합니다.
- TrackInPrevent와 StepPath의 원천 SQL 범위는 기존 의도를 유지했습니다.
- `LOT_TYPE='-'`인 Tip은 모든 LOT_TYPE에 적용되는 wildcard로 처리했습니다. 두 번째 코드가 Tip의 `lot_type`을 항상 `'-'`로 만들기 때문에, 이 처리가 없으면 Tip이 LOT과 매칭되지 않습니다.

## 행 수가 기존 방식과 달라질 수 있는 정상 원인

1. 설비그룹 하나에 여러 EQP가 있어 Step 행이 펼쳐지는 경우
2. 설비 본체 하나에 여러 챔버가 있어 Tip 행이 펼쳐지는 경우
3. exact와 wildcard가 같은 이벤트를 가리켜 우선순위 중복 제거되는 경우
4. 최종 출력 컬럼이 완전히 같은 행이 `DISTINCT`로 제거되는 경우
5. 기존 코드에서 `eqp_id`만으로 결합하던 부분을 `line + eqp_id`로 고쳐 다른 라인의 설비가 잘못 붙는 행이 제거되는 경우

실행 로그에는 주요 조인 전후 행 수, 증폭률, 중복 키 수, 대표 샘플을 기록합니다.

## 메모리 설정

PC 메모리를 모르는 상태이므로 기본은 DuckDB 자동 설정을 사용합니다. 메모리 부족이 발생하면 PowerShell에서 제한을 지정할 수 있습니다.

```powershell
$env:MULTIWIP_DUCKDB_MEMORY_LIMIT="4GB"
$env:MULTIWIP_DUCKDB_THREADS="4"
python build_multiwip_direct_optimized.py --rebuild
```

DuckDB는 메모리 한도를 넘는 중간 연산을 `output/duckdb_temp`로 내보낼 수 있습니다.

## 최초 실행 시 확인할 로그

- `step_eqp_group_amplification`
- `tip_join_amplification`
- `equipment_duplicate_line_eqp_keys`
- `f1_duplicate_rows_removed`
- `f2_duplicate_rows_removed`
- `f3_duplicate_rows_removed`
- `f1/f2/f3_lot_distinct`

증폭률이 비정상적으로 높거나 distinct 제거 건수가 크면 로그의 샘플 키를 기준으로 원천 중복인지 정상적인 설비 후보 확장인지 확인해야 합니다.
