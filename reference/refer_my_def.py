import os
import pandas as pd
import numpy as np
import re
import time
from contextlib import contextmanager
from bisect import bisect_left, bisect_right
from typing import Optional

@contextmanager
def timer(name: str):
    t0 = time.perf_counter()
    yield
    dt = time.perf_counter() - t0
    print(f"[TIMER] {name}: {dt:,.2f} sec")


def get_unique_filename(filepath):
    if not os.path.exists(filepath):
        return filepath
    
    base, ext = os.path.splitext(filepath)
    i = 1
    while True:
        new_path = f"{base}_{i}{ext}"
        if not os.path.exists(new_path):
            return new_path
        i += 1


def unique_concat_sorted(s: pd.Series, sep: str = ",") -> str:
    vals = s.dropna().astype(str).unique()
    if len(vals) == 0:
        return ""
    return sep.join(sorted(vals))


# def join_step_window_by_index(df_mclot, df_step, window=100):
    # step 정렬
    df_step = (
        df_step
        .sort_values(["processid", "stepseq"], kind="mergesort")
        .reset_index(drop=True)
    )

    # processid별 index 매핑
    step_groups = {
        pid: grp.index.to_list()
        for pid, grp in df_step.groupby("processid", sort=False)
    }

    rows = []

    for _, m in df_mclot.iterrows():
        pid = m["proc_id"]
        cur_step = m["step_seq"]

        if pid not in step_groups:
            continue

        idxs = step_groups[pid]

        # 현재 step 위치 찾기
        try:
            cur_pos = df_step.loc[idxs, "stepseq"].tolist().index(cur_step)
        except ValueError:
            continue

        # 🔴 여기만 변경
        before = window          # 앞 100
        after  = window * 2      # 뒤 200

        start = max(0, cur_pos - before)
        end   = min(len(idxs), cur_pos + after + 1)

        target_idxs = idxs[start:end]

        for si in target_idxs:
            s = df_step.loc[si]
            rows.append({
                "lot_id": m["lot_id"],
                "cur_qty": m["cur_qty"],
                "carr_id": m["carr_id"],
                "hot_lot_level": m["hot_lot_level"],
                "cur_line_id": m["cur_line_id"],
                "lot_type": m["lot_type"],
                "status": m["status"],
                "proc_id": m["proc_id"],
                "layerid": s["layerid"],
                "현step": m["step_seq"],
                "category": s["category"],
                "skiprule": s["skiprule"],
                "stepseq": s["stepseq"],
                "descript": s["descript"],
                "recipeid": s["recipeid"],
                "delaytime": s["delaytime"],
                "n2_delay_time_mins": s["n2_delay_time_mins"],
                "areaname": s["areaname"],
                "eqptype": s["eqptype"],
                "stepseq_type": s["stepseq_type"],
                "ff": s["ff"],
                "tt": s["tt"],
                "last_event_date": m["last_event_date"],
            })

    return pd.DataFrame(rows)


def join_step_window_by_index(df_mclot: pd.DataFrame,
                            df_step: pd.DataFrame,
                            window: int = 100) -> pd.DataFrame:
    # step 정렬 + process 내 순서(_step_order) 부여
    df_step = (
        df_step
        .sort_values(["processid", "stepseq"], kind="mergesort")
        .reset_index(drop=True)
        .copy()
    )
    df_step["_step_order"] = df_step.groupby("processid", sort=False).cumcount()

    # stepseq_type 재해석 규칙: 첫 글자가 R이면 메인
    if "stepseq_type" in df_step.columns:
        r_mask = df_step["stepseq"].astype(str).str.startswith("R", na=False)
        df_step.loc[r_mask, "stepseq_type"] = "메인"

    # processid별 index list
    step_groups = {
        pid: grp.index.to_list()
        for pid, grp in df_step.groupby("processid", sort=False)
    }

    rows = []
    for _, m in df_mclot.iterrows():
        pid = m["proc_id"]
        cur_step = m["step_seq"]
        if pid not in step_groups:
            continue

        idxs = step_groups[pid]

        # 현재 step 위치 찾기(해당 process 내부에서의 위치)
        try:
            # 성능/정확성: list.index 사용(기존 유지)
            cur_pos = df_step.loc[idxs, "stepseq"].tolist().index(cur_step)
        except ValueError:
            continue

        before = window
        after = window
        start = max(0, cur_pos - before)
        end = min(len(idxs), cur_pos + after + 1)
        target_idxs = idxs[start:end]

        for si in target_idxs:
            s = df_step.loc[si]

            # step에서 가져와야 하는 값들은 s 기준
            rows.append({
                "lot_id": m["lot_id"],
                "cur_qty": m["cur_qty"],
                "carr_id": m["carr_id"],
                "hot_lot_level": m["hot_lot_level"],
                "cur_line_id": m["cur_line_id"],
                "lot_type": m["lot_type"],
                "status": m["status"],
                "proc_id": m["proc_id"],

                "layerid": s.get("layerid", np.nan),
                # 현step은 mclot의 현step 컬럼이 원래 있으면 그걸 유지
                # 없으면 m["step_seq"]로 채움(당신이 이전에 요구한 규칙)
                "현step": m["현step"] if "현step" in df_mclot.columns else m["step_seq"],

                "category": s.get("category", np.nan),
                "skiprule": s.get("skiprule", np.nan),
                "stepseq": s.get("stepseq", np.nan),
                "descript": s.get("descript", np.nan),
                "recipeid": s.get("recipeid", np.nan),
                "delaytime": s.get("delaytime", np.nan),
                "n2_delay_time_mins": s.get("n2_delay_time_mins", np.nan),
                "areaname": s.get("areaname", np.nan),
                "eqptype": s.get("eqptype", np.nan),
                "stepseq_type": s.get("stepseq_type", np.nan),
                "ff": s.get("ff", np.nan),
                "tt": s.get("tt", np.nan),

                # ✅ 이후 keep_steps_like_finalize에서 “전진” 판단용
                "_step_order": s.get("_step_order", np.nan),

                "last_event_date": m["last_event_date"],
            })

    return pd.DataFrame(rows)


def join_engrlot_ppid_override_recipeid(df_mcstep: pd.DataFrame,
                                        df_engrlotppid: pd.DataFrame) -> pd.DataFrame:
    """
    df_mcstep(A) + df_engrlotppid(B) LEFT JOIN

    기존 기능 유지:
    - A 전체 컬럼 유지
    - B에서 사전지정eqp/사전지정user/사전지정일/사전지정(O) 생성
    - 조인키: A.lot_id = B.lotid, A.stepseq = B.stepseq (그리고 A.proc_id = B.processid가 있으면 그것도)
    - 컬럼 순서 조정

    변경(요청 반영):
    - 기존 A.recipeid는 그대로 유지 (대체 금지)
    - B.newppid가 유효(!= '-' and not null)하면
    '사전지정_ppid' 신규 컬럼으로 추가하고 '사전지정' 컬럼 바로 앞에 배치
    """

    A = df_mcstep.copy()
    B = df_engrlotppid.copy()

    # -----------------------------
    # 1) 조인 키 세팅 (가능한 키는 모두 사용)
    # -----------------------------
    left_on = ["lot_id", "stepseq"]
    right_on = ["lotid", "stepseq"]

    if ("proc_id" in A.columns) and ("processid" in B.columns):
        left_on = ["proc_id"] + left_on
        right_on = ["processid"] + right_on

    # -----------------------------
    # 2) LEFT JOIN
    # -----------------------------
    df = A.merge(
        B,
        how="left",
        left_on=left_on,
        right_on=right_on,
        suffixes=("", "_ep"),
    )

    # -----------------------------
    # 3) 사전지정 관련 컬럼 생성(기존 유지)
    # -----------------------------
    # 유효한 사전지정 여부: newppid가 존재하고 '-'가 아닌 경우
    has_override = df["newppid"].notna() & (df["newppid"] != "-")

    # (변경점) recipeid 대체하지 않음. 대신 사전지정_ppid 신규 컬럼으로 보관
    df["사전지정_ppid"] = np.where(has_override, df["newppid"], None)

    # 기존 요구: ep.eqpid -> "사전지정eqp"
    if "eqpid" in df.columns:
        df["사전지정eqp"] = np.where(has_override, df["eqpid"], None)
    else:
        # B에 eqpid가 없을 때 대비
        df["사전지정eqp"] = None

    # ep.userid -> "사전지정user"
    if "userid" in df.columns:
        df["사전지정user"] = np.where(has_override, df["userid"], None)
    else:
        df["사전지정user"] = None

    # ep.updated -> "사전지정일"
    if "updated" in df.columns:
        df["사전지정일"] = np.where(has_override, df["updated"], None)
    else:
        df["사전지정일"] = None

    # "사전지정" 플래그(O/공백)
    df["사전지정"] = np.where(has_override, "O", "")

    # -----------------------------
    # 4) 컬럼 위치 조정: '사전지정' 바로 앞에 '사전지정_ppid'
    # -----------------------------
    cols = list(df.columns)
    if "사전지정_ppid" in cols and "사전지정" in cols:
        cols.remove("사전지정_ppid")
        insert_pos = cols.index("사전지정")
        cols.insert(insert_pos, "사전지정_ppid")
        df = df[cols]

    # -----------------------------
    # 5) 불필요 조인 컬럼 정리(기존 유지 성격)
    # - 원본 조인키 lotid/processid는 보통 제거
    # - newppid는 사전지정_ppid로 대체되었으니 제거 권장
    # -----------------------------
    drop_candidates = ["lotid", "processid", "newppid"]
    existing_drop = [c for c in drop_candidates if c in df.columns]
    if existing_drop:
        df = df.drop(columns=existing_drop)

    return df


def join_pems_with_rules(
    df_mse: pd.DataFrame,
    df_pems: pd.DataFrame,
    df_selectconnectspec: pd.DataFrame,
    debug: bool = False
) -> pd.DataFrame:
    A = df_mse.copy()
    B = df_pems.copy()
    C = df_selectconnectspec.copy()

    # 필수 컬럼 체크
    need_A = {"proc_id", "lot_id", "stepseq"}
    need_B = {
        "processid", "pems_type", "ecnrule", "lotids", "einecnno", "stepseq",
        "connecttype", "nextstepseq", "pems_eqpids", "pems_chamberids",
        "pems_ppid", "pems_comment", "pems_user"
    }
    need_C = {"firsteinecnno", "nexteinecnno", "selecteinecnno"}

    miss_A = need_A - set(A.columns)
    miss_B = need_B - set(B.columns)
    miss_C = need_C - set(C.columns)
    if len(miss_A) > 0:
        raise KeyError(f"df_mse missing columns: {sorted(miss_A)}")
    if len(miss_B) > 0:
        raise KeyError(f"df_pems missing columns: {sorted(miss_B)}")
    if len(miss_C) > 0:
        raise KeyError(f"df_selectconnectspec missing columns: {sorted(miss_C)}")

    # 문자열 정규화
    A["proc_id"] = A["proc_id"].astype(str).str.strip()
    A["lot_id"] = A["lot_id"].astype(str).str.strip()
    A["stepseq"] = A["stepseq"].astype(str).str.strip()

    B["processid"] = B["processid"].astype(str).str.strip()
    B["pems_type"] = B["pems_type"].astype(str).str.strip()
    B["ecnrule"] = B["ecnrule"].astype(str).str.strip()
    B["lotids"] = B["lotids"].astype(str).str.strip()
    B["einecnno"] = B["einecnno"].astype(str).str.strip()
    B["stepseq"] = B["stepseq"].astype(str).str.strip()

    C["firsteinecnno"] = C["firsteinecnno"].astype(str).str.strip()
    C["nexteinecnno"] = C["nexteinecnno"].astype(str).str.strip()
    C["selecteinecnno"] = C["selecteinecnno"].astype(str).str.strip()

    # A row id
    A = A.reset_index(drop=True)
    A["_aid"] = np.arange(len(A))

    # '.' 바로 앞 1글자 (가장 안전한 방법: 정규식)
    # '1EA081.1' -> '1'
    A["_predot_char"] = A["lot_id"].str.extract(r"(.)\.", expand=False).fillna("")

    # selectconnectspec 매핑 dict
    sel_map = {(r.firsteinecnno, r.nexteinecnno): r.selecteinecnno for r in C.itertuples(index=False)}

    def resolve_by_selectspec(rows: pd.DataFrame) -> pd.Series:
        if len(rows) == 1:
            return rows.iloc[0]

        winner = rows.iloc[0]
        w = winner["einecnno"]

        for i in range(1, len(rows)):
            cand = rows.iloc[i]
            c = cand["einecnno"]

            pick = sel_map.get((w, c))
            if pick is None:
                pick = sel_map.get((c, w))

            if pick == c:
                winner = cand
                w = c
            elif pick == w:
                pass
            else:
                pass

        return winner

    # B 준비
    B = B.reset_index(drop=True)
    B["_b_order"] = np.arange(len(B))

    B_ecn = B[B["pems_type"].isin(["ECN", "RCS"])].copy()
    B_ecn_all = B_ecn[B_ecn["ecnrule"] == "-"].copy()
    B_ecn_spec = B_ecn[B_ecn["ecnrule"] != "-"].copy()

    if not B_ecn_spec.empty:
        B_ecn_spec["_ecn_key_list"] = (
            B_ecn_spec["ecnrule"].str.replace(" ", "", regex=False).str.split(",")
        )
        B_ecn_spec = B_ecn_spec.explode("_ecn_key_list", ignore_index=True)
        B_ecn_spec["_ecn_key_list"] = B_ecn_spec["_ecn_key_list"].fillna("").astype(str).str.strip()

    B_ein = B[B["pems_type"] == "EIN"].copy()
    if not B_ein.empty:
        B_ein["_lot_key_list"] = B_ein["lotids"].astype(str).str.split(",")
        B_ein = B_ein.explode("_lot_key_list", ignore_index=True)
        B_ein["_lot_key_list"] = B_ein["_lot_key_list"].fillna("").astype(str).str.strip()

    # 후보 생성
    candidates_list = []

    # 1-1 ECN/RCS all-lot: proc_id & stepseq
    if not B_ecn_all.empty:
        cand1 = A.merge(
            B_ecn_all,
            how="inner",
            left_on=["proc_id", "stepseq"],
            right_on=["processid", "stepseq"],
            suffixes=("", "_b")
        )
        candidates_list.append(cand1)

    # 1-1 ECN/RCS rule-based: proc_id & stepseq & predot_char
    if not B_ecn_spec.empty:
        cand2 = A.merge(
            B_ecn_spec,
            how="inner",
            left_on=["proc_id", "stepseq", "_predot_char"],
            right_on=["processid", "stepseq", "_ecn_key_list"],
            suffixes=("", "_b")
        )
        candidates_list.append(cand2)

    # 1-2 EIN: proc_id & stepseq & lot_id in lotids
    if not B_ein.empty:
        cand3 = A.merge(
            B_ein,
            how="inner",
            left_on=["proc_id", "stepseq", "lot_id"],
            right_on=["processid", "stepseq", "_lot_key_list"],
            suffixes=("", "_b")
        )
        candidates_list.append(cand3)

    if len(candidates_list) > 0:
        cand = pd.concat(candidates_list, ignore_index=True)
        cand = cand.sort_values(["_aid", "_b_order"], kind="mergesort").reset_index(drop=True)

        picked = (
            cand.groupby("_aid", sort=False, group_keys=False)
                .apply(resolve_by_selectspec)
        ).reset_index(drop=True)

        picked_small = picked[[
            "_aid",
            "einecnno",
            "connecttype", "nextstepseq", "pems_eqpids", "pems_chamberids",
            "pems_ppid", "pems_comment", "pems_user",
        ]].copy()

        out = A.merge(picked_small, how="left", on="_aid")
        out = out.rename(columns={"einecnno": "적용PEMSNO"})

    else:
        out = A.copy()
        for c in ["connecttype", "nextstepseq", "pems_eqpids", "pems_chamberids", "pems_ppid", "pems_comment", "pems_user"]:
            out[c] = np.nan

    out["PEMS"] = out["connecttype"].notna().map(lambda x: "O" if x else "")
    out = out.drop(columns=[c for c in ["_aid", "_predot_char"] if c in out.columns])

    if debug:
        total = len(df_mse)
        joined = (out["PEMS"] == "O").sum()
        print(f"[debug] df_mse rows: {total}")
        print(f"[debug] PEMS joined rows: {joined}")
        print(f"[debug] PEMS not joined rows: {total - joined}")

    return out


def build_df_msep_skip(df_msep: pd.DataFrame) -> pd.DataFrame:
    df = df_msep.copy()

    required = [
        "lot_id", "stepseq", "category", "hot_lot_level", "skiprule",
        "ff", "tt", "stepseq_type",
        "connecttype", "nextstepseq",
        "사전지정",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"df_msep missing columns: {missing}")

    # ===== 스칼라 문자열 강제 =====
    def _to_scalar_str(x) -> str:
        if isinstance(x, pd.Series):
            x = x.iloc[0] if len(x) > 0 else ""
        elif isinstance(x, (list, tuple, np.ndarray)):
            x = x[0] if len(x) > 0 else ""
        if x is None:
            return ""
        try:
            if isinstance(x, float) and np.isnan(x):
                return ""
        except Exception:
            pass
        return str(x).strip()

    for c in ["lot_id", "stepseq", "category", "hot_lot_level", "skiprule", "ff", "tt",
            "stepseq_type", "connecttype", "nextstepseq", "사전지정"]:
        df[c] = df[c].astype("object").map(_to_scalar_str)

    # ===== 정렬(순서 기준) =====
    if "proc_id" in df.columns:
        sort_cols = ["lot_id", "proc_id", "stepseq"]
    else:
        sort_cols = ["lot_id", "stepseq"]
    df = df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

    # ===== lot_id에서 id 글자 추출 =====
    dot_pos = df["lot_id"].str.find(".")
    id_ff_char = np.where(dot_pos >= 1, df["lot_id"].str.slice(dot_pos-1, dot_pos), "")
    id_tt_char = np.where(dot_pos >= 2, df["lot_id"].str.slice(dot_pos-2, dot_pos-1), "")

    # ===== ff/tt 포함 여부 =====
    def contains_token(list_str: str, token: str) -> bool:
        s = _to_scalar_str(list_str)
        t = _to_scalar_str(token)
        if s == "" or s == "-" or t == "":
            return False
        parts = [p.strip() for p in s.split(",")]
        return t in parts

    ff_hit = pd.Series([contains_token(a, b) for a, b in zip(df["ff"], id_ff_char)], index=df.index)
    tt_hit = pd.Series([contains_token(a, b) for a, b in zip(df["tt"], id_tt_char)], index=df.index)
    id_skip = ff_hit | tt_hit

    # ===== hotlot =====
    hot = df["hot_lot_level"].fillna("-")
    cat = df["category"].fillna("")
    hot_in_cat = pd.Series([h in c for h, c in zip(hot.tolist(), cat.tolist())], index=df.index)
    hot_skip = (hot != "-") & (hot != "") & hot_in_cat

    # ===== skiprule =====
    skiprule_skip = (df["skiprule"].fillna("") == "100")

    # ===== 사전지정 =====
    preassigned = (df["사전지정"].fillna("") == "O")

    # ===== PEMS 점프 트리거 =====
    ct_u = df["connecttype"].fillna("").str.upper()
    ns = df["nextstepseq"].fillna("")
    jump_trigger = ct_u.isin(["START", "CONNECT"]) & (ns != "") & (ns != "-")

    # ===== PEMS 점프 구간 생성(2-1) =====
    df["_jump_skip"] = False

    for lot, gidx in df.groupby("lot_id", sort=False).groups.items():
        idxs = list(gidx)
        step_list = [ _to_scalar_str(v) for v in df.loc[idxs, "stepseq"].tolist() ]
        pos_map = {st: i for i, st in enumerate(step_list) if st != ""}

        for local_i, gi in enumerate(idxs):
            if not bool(jump_trigger.iloc[gi]):
                continue

            target = _to_scalar_str(df.at[gi, "nextstepseq"])
            start_pos = local_i + 1

            if target != "" and target != "-" and target in pos_map:
                end_pos = pos_map[target]
                skip_range = range(start_pos, max(start_pos, end_pos))
            else:
                skip_range = range(start_pos, len(idxs))

            for p in skip_range:
                gj = idxs[p]
                # 사전지정 최우선: 점프로도 SKIP 불가
                if bool(preassigned.iloc[gj]):
                    continue
                df.at[gj, "_jump_skip"] = True

    # ===== 기본로직 skip =====
    base_skip = hot_skip | skiprule_skip | id_skip

    # =========================================================
    # 추가로직 0) 비메인(기타) 조건을 "정확히" 반영
    # - stepseq_type == '기타'는 '진행 지정'이 없으면 무조건 SKIP
    # - 진행 지정: (사전지정) OR (connecttype START/CONNECT) OR (해당 lot의 nextstepseq로 지정된 목적지 step)
    # =========================================================

    # lot별 nextstepseq 목적지 세트 만들기(윈도우 안에 보이는 것만이라도)
    df["_is_jump_target"] = False
    for lot, gidx in df.groupby("lot_id", sort=False).groups.items():
        idxs = list(gidx)
        targets = set(
            t for t in df.loc[idxs, "nextstepseq"].tolist()
            if _to_scalar_str(t) not in ["", "-"]
        )
        if not targets:
            continue
        mask = df.index.isin(idxs) & df["stepseq"].isin(targets)
        df.loc[mask, "_is_jump_target"] = True

    pems_designated = ct_u.isin(["START", "CONNECT"]) | df["_is_jump_target"]

    etc = (df["stepseq_type"].fillna("") == "기타")
    etc_skip = etc & (~preassigned) & (~pems_designated)

    # ===== 최종 SKIP 결합 (사전지정은 맨 마지막에 강제 무효 처리) =====
    skip_final = df["_jump_skip"] | base_skip | etc_skip

    # ===== SKIP조건: 1개만 (우선순위 높은 것 1개) =====
    # 우선순위: PEMS_JUMP > 사전지정(진행이라서 표시 안 함) > ETC(비메인) > SKIPRULE100 > HOTLOT > ID(ff/tt)
    reason = pd.Series([""] * len(df), index=df.index, dtype="object")

    # 사전지정이면 어차피 skip_final에서 제거될 것이라 reason은 빈칸 유지
    # 1) PEMS 점프
    reason = reason.mask(df["_jump_skip"], "PEMS_JUMP")

    # 2) 기타(비메인) - PEMS_JUMP가 이미 찍힌 건 유지
    reason = reason.mask((reason == "") & etc_skip, "ETC(비메인)")

    # 3) 기본로직(우선순위: skiprule > hotlot > id)
    reason = reason.mask((reason == "") & skiprule_skip & (~preassigned), "SKIPRULE100")
    reason = reason.mask((reason == "") & hot_skip & (~preassigned), "HOTLOT")
    reason = reason.mask((reason == "") & id_skip & (~preassigned), "ID(ff/tt)")

    # ===== ✅ 최종 우선순위 강제: 사전지정이면 어떤 SKIP도 무효 =====
    skip_final = skip_final & (~preassigned)
    reason = reason.mask(preassigned, "")

    df["SKIP"] = np.where(skip_final, "O", "")
    df["SKIP조건"] = np.where(skip_final, reason, "")

    # 임시 컬럼 제거
    df = df.drop(columns=["_jump_skip", "_is_jump_target"], errors="ignore")

    return df


# def add_continuous_col_v9(df_in: pd.DataFrame) -> pd.DataFrame:
    """
    SKIP=='O' 제거 후 '연속' 컬럼 추가
    sentinel_delaytimes = {'1000000020','1000000000'}

    METRO/MI 규칙(최종 변경 반영):
    - METRO/MI 행은 "후속 중 가장 가까운 (메인 AND areaname NOT IN ('METRO','MI'))"의 delaytime을 상속
    - 상속 delaytime이 sentinel 아니면 METRO/MI도 연속(core)
    - sentinel이면 비연속
    - 후속 대상이 없으면 비연속
    """

    df = df_in.copy()

    required = ["SKIP", "lot_id", "stepseq", "delaytime", "areaname", "stepseq_type"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"missing columns: {missing}")

    # 1) SKIP 제거
    df = df[df["SKIP"].fillna("") != "O"].copy()

    # 2) 정렬
    sort_cols = ["lot_id", "stepseq"]
    if "proc_id" in df.columns:
        sort_cols = ["lot_id", "proc_id", "stepseq"]
    df = df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

    # 3) 문자열 정규화
    df["areaname"] = df["areaname"].astype(str).str.strip()
    df["stepseq_type"] = df["stepseq_type"].astype(str).str.strip()

    # 4) delaytime 정규화 (".0" 포함)
    def norm_delay(x) -> str:
        if pd.isna(x):
            return ""
        if isinstance(x, (int, np.integer)):
            return str(int(x))
        if isinstance(x, (float, np.floating)):
            return str(int(x)) if float(x).is_integer() else str(x).strip()

        s = str(x).strip()
        if s == "":
            return ""
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        try:
            f = float(s)
            if f.is_integer():
                return str(int(f))
        except Exception:
            pass
        return s

    delay_norm = df["delaytime"].map(norm_delay)

    sentinel = {"1000000020", "1000000000"}

    is_metro = df["areaname"].isin(["METRO", "MI"])
    is_main = (df["stepseq_type"] == "메인")
    is_main_non_metro = is_main & (~is_metro) # ✅ 후속 참조 대상

    # 그룹키: proc_id가 있으면 같이 묶는 게 안전(경로 섞임 방지)
    group_keys = ["lot_id"]
    if "proc_id" in df.columns:
        group_keys.append("proc_id")

    # 5) METRO/MI 상속: "후속의 가장 가까운 메인&비METRO/MI" delay
    inherited_delay_for_metro = pd.Series([""] * len(df), index=df.index, dtype="object")

    for _, idxs in df.groupby(group_keys, sort=False).groups.items():
        idxs = list(idxs)
        next_target_delay = "" # 후속에서 가장 가까운 (메인&비METRO/MI)의 유효 delay

        for gi in reversed(idxs):
            # ✅ 후속 탐색 대상: 메인 & 비METRO/MI
            if bool(is_main_non_metro.loc[gi]):
                d = delay_norm.loc[gi]
                if d is not None:
                    d = str(d).strip()
                    # 유효값만 채택
                    if d not in ["", "-", "nan", "None"]:
                        next_target_delay = d

            # METRO/MI면 그 시점의 후속 타겟 delay를 상속
            if bool(is_metro.loc[gi]):
                inherited_delay_for_metro.loc[gi] = next_target_delay

    # 6) effective delay: METRO/MI는 상속값, 그 외는 자기값
    effective_delay = delay_norm.where(~is_metro, inherited_delay_for_metro)

    # 7) core 판정: effective_delay가 sentinel이 아니고 비어있지 않으면 core
    is_core = (effective_delay != "") & (~effective_delay.isin(sentinel))

    # 8) 연속 라벨링
    df["연속"] = ""

    for _, idxs in df.groupby(group_keys, sort=False).groups.items():
        idxs = list(idxs)
        n = len(idxs)
        i = 0

        while i < n:
            gi = idxs[i]
            if not bool(is_core.loc[gi]):
                i += 1
                continue

            end_pos = i
            last_core_pos = i
            j = i

            while j + 1 < n:
                nxt = idxs[j + 1]

                if bool(is_core.loc[nxt]):
                    j += 1
                    end_pos = j
                    last_core_pos = j
                    continue

                # METRO/MI는 "상속 후 core인 경우만" 덩어리 내부로 포함 가능
                if bool(is_metro.loc[nxt]) and bool(is_core.loc[nxt]):
                    j += 1
                    end_pos = j
                    continue

                break

            first_core_pos = i
            last_core_idx = idxs[last_core_pos]

            # 연속첫: 첫 core 직전이 (메인&비METRO/MI) 이고, 직전이 core가 아닐 때만
            prev_pos = first_core_pos - 1
            if prev_pos >= 0:
                prev_idx = idxs[prev_pos]
                if bool(is_main_non_metro.loc[prev_idx]) and (not bool(is_core.loc[prev_idx])):
                    df.at[prev_idx, "연속"] = "연속첫"
                    seg_start_pos = prev_pos
                else:
                    seg_start_pos = first_core_pos
            else:
                seg_start_pos = first_core_pos

            for p in range(seg_start_pos, end_pos + 1):
                idx = idxs[p]
                if df.at[idx, "연속"] == "연속첫":
                    continue
                if bool(is_core.loc[idx]):
                    df.at[idx, "연속"] = "연속끝" if idx == last_core_idx else "연속"
                else:
                    # 덩어리 내부로 포함된 METRO/MI는 is_core True만 허용했으므로 여기 안 옴
                    df.at[idx, "연속"] = ""

            i = end_pos + 1

    return df


def add_continuous_col_v9(df_in: pd.DataFrame) -> pd.DataFrame:
    df = df_in.copy()

    required = ["SKIP", "lot_id", "stepseq", "delaytime", "areaname", "stepseq_type"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"missing columns: {missing}")

    # ✅ (변경) SKIP 행을 삭제하지 않는다
    is_skip = (df["SKIP"].fillna("") == "O")

    # 2) 정렬
    sort_cols = ["lot_id", "stepseq"]
    if "proc_id" in df.columns:
        sort_cols = ["lot_id", "proc_id", "stepseq"]
    df = df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

    # 3) 문자열 정규화
    df["areaname"] = df["areaname"].astype(str).str.strip()
    df["stepseq_type"] = df["stepseq_type"].astype(str).str.strip()

    # 4) delaytime 정규화
    def norm_delay(x) -> str:
        if pd.isna(x):
            return ""
        if isinstance(x, (int, np.integer)):
            return str(int(x))
        if isinstance(x, (float, np.floating)):
            return str(int(x)) if float(x).is_integer() else str(x).strip()
        s = str(x).strip()
        if s == "":
            return ""
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        try:
            f = float(s)
            if f.is_integer():
                return str(int(f))
        except Exception:
            pass
        return s

    delay_norm = df["delaytime"].map(norm_delay)

    sentinel = {"1000000020", "1000000000"}
    is_metro = df["areaname"].isin(["METRO", "MI"])
    is_main = (df["stepseq_type"] == "메인")
    is_main_non_metro = is_main & (~is_metro)

    group_keys = ["lot_id"]
    if "proc_id" in df.columns:
        group_keys.append("proc_id")

    # 5) METRO/MI 상속
    inherited_delay_for_metro = pd.Series([""] * len(df), index=df.index, dtype="object")
    for _, idxs in df.groupby(group_keys, sort=False).groups.items():
        idxs = list(idxs)
        next_target_delay = ""
        for gi in reversed(idxs):
            if bool(is_main_non_metro.loc[gi]):
                d = delay_norm.loc[gi]
                d = "" if d is None else str(d).strip()
                if d not in ["", "-", "nan", "None"]:
                    next_target_delay = d
            if bool(is_metro.loc[gi]):
                inherited_delay_for_metro.loc[gi] = next_target_delay

    effective_delay = delay_norm.where(~is_metro, inherited_delay_for_metro)

    # ✅ (변경) core 판정에서 SKIP(O)는 core 불가
    is_core = (effective_delay != "") & (~effective_delay.isin(sentinel)) & (~is_skip)

    # 8) 연속 라벨링 (기존 로직 유지)
    df["연속"] = ""
    for _, idxs in df.groupby(group_keys, sort=False).groups.items():
        idxs = list(idxs)
        n = len(idxs)
        i = 0
        while i < n:
            gi = idxs[i]
            if not bool(is_core.loc[gi]):
                i += 1
                continue

            end_pos = i
            last_core_pos = i
            j = i
            while j + 1 < n:
                nxt = idxs[j + 1]
                if bool(is_core.loc[nxt]):
                    j += 1
                    end_pos = j
                    last_core_pos = j
                    continue
                if bool(is_metro.loc[nxt]) and bool(is_core.loc[nxt]):
                    j += 1
                    end_pos = j
                    continue
                break

            first_core_pos = i
            last_core_idx = idxs[last_core_pos]

            prev_pos = first_core_pos - 1
            if prev_pos >= 0:
                prev_idx = idxs[prev_pos]
                if bool(is_main_non_metro.loc[prev_idx]) and (not bool(is_core.loc[prev_idx])):
                    df.at[prev_idx, "연속"] = "연속첫"
                    seg_start_pos = prev_pos
                else:
                    seg_start_pos = first_core_pos
            else:
                seg_start_pos = first_core_pos

            for p in range(seg_start_pos, end_pos + 1):
                idx = idxs[p]
                if df.at[idx, "연속"] == "연속첫":
                    continue
                if bool(is_core.loc[idx]):
                    df.at[idx, "연속"] = "연속끝" if idx == last_core_idx else "연속"
                else:
                    df.at[idx, "연속"] = ""
            i = end_pos + 1

    return df


def keep_steps_like_finalize(df_in: pd.DataFrame) -> pd.DataFrame:
    """
    add_continuous_col_v9 이후 df를 입력으로 받아,
    finalize_dashboard_rows의 "현스텝~연속끝" 남기기 로직을 미리 적용.

    추가 요구 반영:
    - df_in의 현step이 SKIP('O')이면,
      현step 이후(next) 비SKIP 중 가장 가까운 step을 "현step"으로 대체.
    - stepseq는 그 새로운 현step 기준으로 (연속이면 ~연속끝), 아니면 1행만.
    """

    df = df_in.copy()

    # 필수 컬럼 보정
    need_cols = ["lot_id", "stepseq", "현step", "SKIP", "연속"]
    for c in need_cols:
        if c not in df.columns:
            df[c] = np.nan

    # ✅ 반드시 있어야 “과거 스텝 잡는 회귀”가 사라짐
    if "_step_order" not in df.columns:
        raise KeyError("keep_steps_like_finalize: missing column '_step_order' (join_step_window_by_index에서 반드시 생성돼야 함)")

    # 정렬: lot + proc(있으면) + step order
    group_keys = ["lot_id"]
    if "proc_id" in df.columns:
        group_keys.append("proc_id")

    df["_step_order"] = pd.to_numeric(df["_step_order"], errors="coerce")
    df = df.sort_values(group_keys + ["_step_order"], kind="mergesort").reset_index(drop=True)

    out_parts = []

    for _, g in df.groupby(group_keys, sort=False):
        g = g.copy()

        # 현step 후보(원본)
        cur_step = g["현step"].iloc[0]
        cur_step = "" if pd.isna(cur_step) else str(cur_step).strip()

        # 현재 step row 찾기
        cur_rows = g.index[g["stepseq"].astype(str).str.strip() == cur_step].tolist()

        # 현step row가 없으면: 이 lot은 건드리지 않고 1행만 남겨서 lot 보존
        if not cur_rows:
            # lot이 사라지지 않게 최소 1행 유지
            out_parts.append(g.iloc[[0]])
            continue

        cur_idx = cur_rows[0]
        cur_order = g.at[cur_idx, "_step_order"]

        # 현step이 SKIP이면 다음 비SKIP을 현step으로 대체
        cur_is_skip = (str(g.at[cur_idx, "SKIP"]).strip() == "O")

        if cur_is_skip:
            # "현step 이후(next)"에서 비SKIP 찾기(전진만 허용)
            cand = g[(g["_step_order"] > cur_order) & (g["SKIP"].fillna("").astype(str).str.strip() != "O")]
            if not cand.empty:
                new_cur_idx = cand.index[0]
                new_cur_step = str(g.at[new_cur_idx, "stepseq"]).strip()
                # ✅ 현step 컬럼을 대체(그룹 전체 동일하게)
                g["현step"] = new_cur_step
                cur_idx = new_cur_idx
                cur_order = g.at[cur_idx, "_step_order"]
                cur_step = new_cur_step
            # cand가 비면: 대체 불가 → 원본 현step 유지(그래도 lot은 유지)

        # 이제 finalize와 동일한 keep 로직: "현step~연속끝" or "현step만"
        label = str(g.at[cur_idx, "연속"]).strip()

        if label in ["연속첫", "연속", "연속끝"]:
            # 현step부터 전진하며 연속끝까지(연속 라벨 유지되는 동안)
            keep_idxs = [cur_idx]
            # cur_idx 이후만 탐색
            forward = g[g["_step_order"] > cur_order]
            for ii in forward.index.tolist():
                lb = str(g.at[ii, "연속"]).strip()
                if lb in ["연속첫", "연속", "연속끝"]:
                    keep_idxs.append(ii)
                    if lb == "연속끝":
                        break
                else:
                    break
            out_parts.append(g.loc[keep_idxs])
        else:
            out_parts.append(g.loc[[cur_idx]])

    out = pd.concat(out_parts, ignore_index=True)

    # 임시 컬럼 제거(원하면 유지 가능)
    out = out.drop(columns=[], errors="ignore")
    return out



def join_tkinprevent_with_issues(df_msep_skip_c: pd.DataFrame,
                                df_tkinprevent: pd.DataFrame) -> pd.DataFrame:
    """
    A = df_msep_skip_c
    B = df_tkinprevent
    - 조인 전 A.recipeid를 우선순위로 대체:
        recipeid_eff = ifnull(사전지정_ppid, ifnull(pems_ppid, recipeid))
      (단 '-' / '' / NaN은 무효)
    - 조인조건:
        A.proc_id = B.process
        A.stepseq = B.step
        A.recipeid_eff = B.ppid
    - 결과에는 B.process/B.step/B.ppid는 남기지 않음(조인키 drop)
    - B에서 추가되는 컬럼은 요구된 ADD COLUMNS에 필요한 것만:
        eqpcham, eqpid, batch_kind, prevent, type_body, type_cham,
        eqpstatus_body, eqpstatus_cham, chamberid, eqpline
      + eqpstatus(파생)
    - 신규컬럼 1~10은 (lot_id, stepseq) 그룹 기준으로 계산해서 모든 행에 반복 표기
    """
    A = df_msep_skip_c.copy()
    B = df_tkinprevent.copy()

    # ---------------------------
    # 공통 유틸: '-', '', NaN -> NaN 처리
    # ---------------------------
    def clean_series(s: pd.Series) -> pd.Series:
        s = s.astype("object")
        s = s.replace(["", " ", " ", "\u00a0", "\u00a0 ", "-", "nan", "None"], np.nan)
        s = s.infer_objects(copy=False) # ✅ FutureWarning(downcasting) 방지: 과거 동작 유지
        return s

    # ---------------------------
    # 0) recipeid_eff 만들기
    # ---------------------------
    for c in ["recipeid", "사전지정_ppid", "pems_ppid"]:
        if c not in A.columns:
            A[c] = np.nan
    A["recipeid"] = clean_series(A["recipeid"])
    A["사전지정_ppid"] = clean_series(A["사전지정_ppid"])
    A["pems_ppid"] = clean_series(A["pems_ppid"])
    A["recipeid_eff"] = A["사전지정_ppid"].combine_first(A["pems_ppid"]).combine_first(A["recipeid"])

    # ---------------------------
    # 1) B 컬럼 subset (필요한 것만)
    # ---------------------------
    # B 조인키 컬럼명 확정: process/step/ppid
    # (이미 확인: process 사용)
    need_cols = [
        "process", "step", "ppid",
        "eqpcham", "eqpid", "batch_kind", "prevent",
        "type_body", "type_cham",
        "eqpstatus_body", "eqpstatus_cham",
        "chamberid", "eqpline"
    ]
    missing = [c for c in ["process", "step", "ppid"] if c not in B.columns]
    if missing:
        raise KeyError(f"df_tkinprevent에 조인키 컬럼이 없습니다: {missing}")

    # 없는 add-column은 빈 컬럼으로 생성(안전)
    for c in need_cols:
        if c not in B.columns:
            B[c] = np.nan
    B = B[need_cols].copy()

    # ---------------------------
    # 2) eqpid가 eqpid_b로 되는 충돌 방지
    # - A에 eqpid가 이미 있으면(과거 단계에서) 제거 후 조인
    # ---------------------------
    if "eqpid" in A.columns:
        A = A.drop(columns=["eqpid"])
    if "eqpcham" in A.columns:
        A = A.drop(columns=["eqpcham"])

    # ---------------------------
    # 3) LEFT JOIN
    # ---------------------------
    df = A.merge(
        B,
        how="left",
        left_on=["proc_id", "stepseq", "recipeid_eff"],
        right_on=["process", "step", "ppid"],
        suffixes=("", "_b") # 이제 B쪽 충돌이 사실상 없음
    )

    # ---------------------------
    # 4) eqpstatus 생성
    # ---------------------------
    for c in ["eqpstatus_body", "eqpstatus_cham", "chamberid",
              "prevent", "type_body", "type_cham", "eqpline",
              "eqpid", "eqpcham"]:
        df[c] = clean_series(df[c])

    bad_status = {"LOCAL", "PM", "DOWN"}
    cond1 = df["eqpstatus_cham"].isna()
    cond2 = df["chamberid"].notna() & df["eqpstatus_body"].isin(list(bad_status))
    df["eqpstatus"] = np.where(
        cond1, df["eqpstatus_body"],
        np.where(cond2, df["eqpstatus_body"], df["eqpstatus_cham"])
    )

    # ---------------------------
    # 5) 신규 1~4 계산을 위한 eqpid/eqpcham 대체 규칙
    # - 계산용 컬럼은 만들되, 마지막에 drop 처리
    # ---------------------------
    # 사전지정 eqp 컬럼명은 이전 단계 정의를 존중: "사전지정eqp"
    if "사전지정eqp" not in df.columns:
        # 혹시 다른 이름이면 대응
        if "사전지정_eqp" in df.columns:
            df["사전지정eqp"] = df["사전지정_eqp"]
        else:
            df["사전지정eqp"] = np.nan

    for c in ["pems_eqpids", "pems_chamberids"]:
        if c not in df.columns:
            df[c] = np.nan

    df["사전지정eqp"] = clean_series(df["사전지정eqp"])
    df["pems_eqpids"] = clean_series(df["pems_eqpids"])
    df["pems_chamberids"] = clean_series(df["pems_chamberids"])
    df["eqpid"] = clean_series(df["eqpid"])
    df["eqpcham"] = clean_series(df["eqpcham"])

    # ✅ 핵심: 이제 df["eqpid"]가 B.eqpid로 들어와 있음
    df["eqpid_eff"] = df["사전지정eqp"].combine_first(df["pems_eqpids"]).combine_first(df["eqpid"])

    # ✅ combine_first 경고 방지: cham_base도 먼저 clean해서 빈값/타입 섞임 제거
    cham_base = df["eqpcham"].combine_first(df["eqpid"])
    cham_base = clean_series(cham_base)

    df["eqpcham_eff"] = df["사전지정eqp"].combine_first(df["pems_chamberids"]).combine_first(cham_base)

    # ---------------------------
    # group key
    # ---------------------------
    if "lot_id" not in df.columns:
        raise KeyError("A에 lot_id가 없습니다.")
    gkeys = ["lot_id", "stepseq"]

    # ---------------------------
    # unique concatenate
    # ---------------------------
    def uniq_concat(values):
        seen = set()
        out = []
        for v in values:
            if pd.isna(v):
                continue
            s = str(v).strip()
            if s == "" or s == "-":
                continue
            if s not in seen:
                seen.add(s)
                out.append(s)
        return ", ".join(out)

    is_pfr1 = (df["eqpline"] == "PFR1")

    # 1) EQPGROUP
    eqpgroup = df.loc[is_pfr1].groupby(gkeys)["eqpid_eff"].apply(uniq_concat)
    df["EQPGROUP"] = df.set_index(gkeys).index.map(eqpgroup).fillna("")

    # 2) 호환등록
    compat = df.loc[~is_pfr1].groupby(gkeys)["eqpid_eff"].apply(uniq_concat)
    df["호환등록"] = df.set_index(gkeys).index.map(compat).fillna("")

    # 3) EQPGROUP(CHAM)
    eqpgroup_ch = df.loc[is_pfr1].groupby(gkeys)["eqpcham_eff"].apply(uniq_concat)
    df["EQPGROUP(CHAM)"] = df.set_index(gkeys).index.map(eqpgroup_ch).fillna("")

    # 4) 호환등록(CHAM)
    compat_ch = df.loc[~is_pfr1].groupby(gkeys)["eqpcham_eff"].apply(uniq_concat)
    df["호환등록(CHAM)"] = df.set_index(gkeys).index.map(compat_ch).fillna("")

    # ---------------------------
    # 5) 스텝이슈 (PFR1)
    # ---------------------------
    prevent_flag = (df["prevent"] == "PREVENT")
    pfr1_total = df.loc[is_pfr1].groupby(gkeys).size()
    pfr1_issue = df.loc[is_pfr1 & (df["eqpstatus"].isin(list(bad_status)) | prevent_flag)].groupby(gkeys).size()

    def step_issue_label(tot, iss):
        if tot > 0 and iss == tot:
            return "ALL ISSUE"
        if tot > 0 and 0 < iss < tot:
            return "일부 ISSUE"
        return ""

    step_issue = {}
    for k, tot in pfr1_total.items():
        iss = int(pfr1_issue.get(k, 0))
        step_issue[k] = step_issue_label(int(tot), iss)
    df["스텝이슈"] = df.set_index(gkeys).index.map(step_issue).fillna("")

    # ---------------------------
    # 6) 설비이슈 / 9) 호환_설비이슈
    # - null 방지: 항상 문자열 반환, 없으면 ""
    # ---------------------------
    def build_status_issue(sub_df, pfr1=True):
        sub = sub_df[sub_df["eqpline"] == "PFR1"] if pfr1 else sub_df[sub_df["eqpline"] != "PFR1"]
        d = {"LOCAL": set(), "PM": set(), "DOWN": set()}
        for _, r in sub.iterrows():
            b = r.get("eqpstatus_body")
            c = r.get("eqpstatus_cham")
            if b in bad_status:
                v = r.get("eqpid_eff")
                if not pd.isna(v):
                    d[b].add(str(v).strip())
            if c in bad_status:
                v = r.get("eqpcham_eff")
                if not pd.isna(v):
                    d[c].add(str(v).strip())
        parts = []
        for s in ["LOCAL", "PM", "DOWN"]:
            items = sorted([x for x in d[s] if x not in ["", "-"]])
            if items:
                parts.append(f"{s}: {', '.join(items)}")
        return ", ".join(parts) if parts else ""

    equip_issue_pfr1 = df.groupby(gkeys, sort=False).apply(lambda x: build_status_issue(x, pfr1=True))
    equip_issue_compat = df.groupby(gkeys, sort=False).apply(lambda x: build_status_issue(x, pfr1=False))
    df["설비이슈"] = df.set_index(gkeys).index.map(equip_issue_pfr1).fillna("")
    df["호환_설비이슈"] = df.set_index(gkeys).index.map(equip_issue_compat).fillna("")

    # ---------------------------
    # 7) TIP이슈 / 10) 호환_TIP이슈
    # ---------------------------
    def build_tip_issue(sub_df, pfr1=True):
        sub = sub_df[sub_df["eqpline"] == "PFR1"] if pfr1 else sub_df[sub_df["eqpline"] != "PFR1"]

        # 참조 설계 유지 + 존재보장
        if "_eqpcham_base" not in sub.columns:
            sub = sub.copy()
            sub["_eqpcham_base"] = sub["eqpcham"].where(
                sub["eqpcham"].notna() & (sub["eqpcham"] != "-"),
                sub["eqpid"]
            )

        prevent = sub["prevent"].fillna("")
        type_body = sub["type_body"].fillna("")
        type_cham = sub["type_cham"].fillna("")
        m_prevent = (prevent == "PREVENT")
        eqpid = sub["eqpid"].fillna("")
        eqpcham_base = sub["_eqpcham_base"].fillna("")

        vals1 = eqpid[m_prevent & (type_body == "PREVENT")].tolist()
        vals2 = eqpcham_base[m_prevent & (type_cham == "PREVENT")].tolist()

        sset = set()
        for v in vals1 + vals2:
            v = str(v).strip()
            if v and v != "-":
                sset.add(v)

        if not sset:
            return ""
        return "PREVENT: " + ", ".join(sorted(sset))

    tip_issue_pfr1 = df.groupby(gkeys, sort=False).apply(lambda x: build_tip_issue(x, pfr1=True))
    tip_issue_compat = df.groupby(gkeys, sort=False).apply(lambda x: build_tip_issue(x, pfr1=False))
    df["TIP이슈"] = df.set_index(gkeys).index.map(tip_issue_pfr1).fillna("")
    df["호환_TIP이슈"] = df.set_index(gkeys).index.map(tip_issue_compat).fillna("")

    # ---------------------------
    # 8) 호환이슈 (eqpline != PFR1) : 5번 로직 동일
    # ---------------------------
    non_total = df.loc[~is_pfr1].groupby(gkeys).size()
    non_issue = df.loc[(~is_pfr1) & (df["eqpstatus"].isin(list(bad_status)) | prevent_flag)].groupby(gkeys).size()

    compat_issue = {}
    for k, tot in non_total.items():
        iss = int(non_issue.get(k, 0))
        compat_issue[k] = step_issue_label(int(tot), iss)
    df["호환이슈"] = df.set_index(gkeys).index.map(compat_issue).fillna("")

    # ---------------------------
    # 6-1) 요청: 조인키(B.process/B.step/B.ppid) 제거
    # 6-2) 계산용 컬럼 제거
    # ---------------------------
    drop_cols = []
    for c in ["process", "step", "ppid", "recipeid_eff", "eqpid_eff", "eqpcham_eff"]:
        if c in df.columns:
            drop_cols.append(c)
    if drop_cols:
        df = df.drop(columns=drop_cols)

    return df
