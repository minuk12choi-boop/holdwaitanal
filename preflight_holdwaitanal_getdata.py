# -*- coding: utf-8 -*-
r"""
7_holdwaitanal 사전 점검 스크립트.

목적
- DuckDB를 import/install 하기 전에 bigdataquery.getData 연결 상태를 확인한다.
- getData 반환형이 pandas DataFrame인지 확인한다.
- chunk/Arrow/iterator 관련 옵션이 함수 시그니처나 소스에 노출되는지 확인한다.
- 실제 대형 조회 없이 원천 테이블별 LIMIT 3 조회로 접근 가능 여부와 컬럼명을 확인한다.

실행
    cd D:\PERSONAL_SPACE\SW\python\7_holdwaitanal
    python preflight_holdwaitanal_getdata.py

이 스크립트는 duckdb를 import하지 않는다.
"""
from __future__ import annotations

import inspect
import sys
from pprint import pprint

try:
    import pandas as pd
except Exception:
    pd = None

try:
    import bigdataquery
    from bigdataquery import getData
except Exception as exc:
    print("[FAIL] bigdataquery/getData import 실패")
    print(repr(exc))
    raise SystemExit(1)


TEST_QUERIES = {
    "equipment": """
        SELECT line_id, origin_line_id, batch_kind, eqp_id,
               eqp_status, tool_kind, eqp_status_change_time, impala_insert_time
        FROM MOS_KH_SMI.SMIMES_MI_EQUIPMENT
        WHERE line_id IN ('KFR7', 'PFR1')
        LIMIT 3
    """,
    "eqp_group": """
        SELECT line_id, eqp_group_name, eqp_id, impala_insert_time
        FROM MOS_KH_SMI.SMIMES_MI_EQP_GROUP_LIST
        WHERE line_id IN ('KFR7', 'PFR1')
        LIMIT 3
    """,
    "kfr7_tip": """
        SELECT process, step, ppid, eqpid, chamberid,
               type, checkcount, tkin_count, updated, eventtime
        FROM MOS_KH_SMI.SMICDC_NRDK_TRACKINPREVENT
        WHERE owner IN ('LEVEL1', 'PHOTO_LEVEL1')
        LIMIT 3
    """,
    "pfr1_tip": """
        SELECT process, step, ppid, eqpid, chamberid,
               type, checkcount, tkin_count, updated, eventtime
        FROM MOS_KH_SMI.SMICDC_P3NRD_TRACKINPREVENT
        WHERE owner IN ('LEVEL1', 'PHOTO_LEVEL1')
        LIMIT 3
    """,
    "kfr7_step_path": """
        SELECT lot_id, order_seq, proc_id, step_seq, step_desc, step_level,
               step_skip_yn, delay_step_type, delay_time_mins, layer_id,
               eqp_type, eqp_group_id, recipe_id, ext_1st_vals, tkin_type_detail
        FROM MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_PATH
        LIMIT 3
    """,
    "pfr1_step_path": """
        SELECT lot_id, order_seq, proc_id, step_seq, step_desc, step_level,
               step_skip_yn, delay_step_type, delay_time_mins, layer_id,
               eqp_type, eqp_group_id, recipe_id, ext_1st_vals, tkin_type_detail
        FROM MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_PATH
        LIMIT 3
    """,
}


def inspect_getdata():
    print("\n=== 1. getData 함수 정보 ===")
    print("bigdataquery module:", getattr(bigdataquery, "__file__", None))
    print("getData type:", type(getData))
    try:
        sig = inspect.signature(getData)
        print("signature:", sig)
        params = {str(p).lower() for p in sig.parameters}
        capability_names = {
            "chunksize", "chunk_size", "batch_size", "fetch_size",
            "as_arrow", "return_arrow", "arrow", "iterator", "stream"
        }
        print("chunk/Arrow/iterator 후보 인자:", sorted(params & capability_names) or "없음")
    except Exception as exc:
        print("signature 확인 실패:", repr(exc))

    try:
        src = inspect.getsource(getData)
        keywords = [
            "chunksize", "chunk_size", "batch_size", "fetch_size", "fetchmany",
            "arrow", "fetch_record_batch", "iterator", "yield"
        ]
        found = {k: (k in src.lower()) for k in keywords}
        print("소스 키워드:")
        pprint(found)
    except Exception as exc:
        print("getData 소스 확인 불가:", repr(exc))


def describe_result(label, result):
    print(f"[{label}] return type = {type(result)}")
    print(f"[{label}] module      = {type(result).__module__}")
    print(f"[{label}] shape       = {getattr(result, 'shape', None)}")
    print(f"[{label}] columns     = {list(getattr(result, 'columns', []))}")
    print(f"[{label}] iterator?   = {hasattr(result, '__next__')}")
    for attr in ("to_arrow", "arrow", "fetch_record_batch", "fetchmany"):
        print(f"[{label}] has {attr:<18} = {hasattr(result, attr)}")
    if pd is not None and isinstance(result, pd.DataFrame):
        try:
            mem = result.memory_usage(index=True, deep=True).sum()
            print(f"[{label}] pandas memory = {mem:,} bytes")
        except Exception:
            pass
        print(result.head(3).to_string(index=False))


def run_query(label, query):
    print(f"\n=== 테스트 조회: {label} ===")
    result = getData(param=query, convert_type=True, verbose=True)
    describe_result(label, result)
    return result


def main():
    inspect_getdata()

    failures = []
    return_types = {}
    for label, query in TEST_QUERIES.items():
        try:
            result = run_query(label, query)
            return_types[label] = str(type(result))
            del result
        except Exception as exc:
            failures.append((label, repr(exc)))
            print(f"[FAIL] {label}: {exc!r}")

    print("\n=== 결과 ===")
    if return_types:
        print("성공한 반환형:")
        pprint(return_types)
    if failures:
        print("실패 항목:")
        pprint(failures)
        raise SystemExit(2)

    print("[PASS] 대형 조회 없이 원천 접근 및 getData 기본 반환형 점검 완료")
    print("이 결과를 전달해주시면 chunk/Arrow 사용 가능 여부까지 판단할 수 있습니다.")


if __name__ == "__main__":
    main()
