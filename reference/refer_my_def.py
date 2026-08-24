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



def finalize_dashboard_rows(df_msep_skip_ct: pd.DataFrame) -> pd.DataFrame:
    """
    입력: df_msep_skip_ct (join_tkinprevent_with_issues 결과)
    출력: 대시보드용 축약 DF
    적용 조건:
    1) lot별 현스텝만 + (연속공정이면 현스텝~연속끝까지)만 남김 # ✅ 상위 단계(keep_steps_like_finalize)에서 이미 수행됨
    2) tkinprevent 조인으로 늘어난 행 축약:
       over(lot_id, stepseq, recipeid)에서 1행만 남김 (단 1번 유지)
    3) recipeid를 ifnull(pems_ppid, ifnull(사전지정_ppid, recipeid)) 로 재정의
       ('-'/'', NaN 무효)
    4) 사전지정_eqpid 또는 ifnull(pems_chamberids, pems_eqpids) 존재 시:
       진행가능 설비path 및 이슈 컬럼을 "대체값과 매칭되는 path" 기준으로 재집계
    5) status 대체:
       status == 'WAIT' and 스텝이슈 == 'ALL ISSUE' -> 'WAIT(진행불가)'
    6) 최종 컬럼/순서 맞춤
    """
    df = df_msep_skip_ct.copy()
    # -------------------------
    # 유틸: 문자열 null 정리
    # -------------------------
    def _clean_obj(x):
        if pd.isna(x):
            return np.nan
        s = str(x).strip()
        if s in ["", "-", "nan", "None"]:
            return np.nan
        return s

    def clean_col(c):
        if c not in df.columns:
            df[c] = np.nan
        df[c] = df[c].map(_clean_obj)

    # 필요한 컬럼들 안전 생성/정리
    base_cols = [
        "lot_id", "stepseq", "status", "연속", "recipeid", "사전지정_ppid", "pems_ppid",
        "사전지정eqp", "pems_eqpids", "pems_chamberids",
        "eqpid", "eqpcham", "eqpline",
        "eqpstatus", "eqpstatus_body", "eqpstatus_cham",
        "prevent", "type_body", "type_cham",
        "스텝이슈", "호환이슈",
        "EQPGROUP", "호환등록", "EQPGROUP(CHAM)", "호환등록(CHAM)",
        "설비이슈", "호환_설비이슈", "TIP이슈", "호환_TIP이슈",
        "현step", "delaytime", "n2_delay_time_mins"
    ]
    for c in base_cols:
        clean_col(c)

    # -------------------------
    # 3) recipeid 재정의: pems_ppid > 사전지정_ppid > recipeid
    # -------------------------
    df["recipeid_new"] = df["pems_ppid"].combine_first(df["사전지정_ppid"]).combine_first(df["recipeid"])
    # 최종 recipeid를 이걸로 교체
    df["recipeid"] = df["recipeid_new"]
    df.drop(columns=["recipeid_new"], inplace=True)

    # -------------------------
    # 1) 현스텝~연속끝만 남기기
    # ✅ 상위 단계에서 이미 df가 해당 조건으로 필터링되어 들어온다고 가정함
    # -------------------------

    # -------------------------
    # 4) 사전지정/PEMS 존재 시 이슈/그룹을 "대체값 매칭 path" 기준으로 재집계
    #
    # 정의:
    # - eqpid_sel = ifnull(사전지정eqp, ifnull(pems_eqpids, eqpid))
    # - eqpcham_sel= ifnull(사전지정eqp, ifnull(pems_chamberids, ifnull(eqpcham, eqpid)))
    #
    # 존재 시(사전지정eqp 또는 pems_eqpids/pems_chamberids):
    # - 해당 그룹(lot_id, stepseq, recipeid)에서
    # B에서 조인된 행 중 eqpid == eqpid_sel 또는 eqpcham(없으면 eqpid) == eqpcham_sel 에 매칭되는 행만 남겨서
    # EQPGROUP/EQPGROUP(CHAM)/이슈 컬럼 재집계
    # - 존재하지 않으면 기존 계산 유지
    # -------------------------
    def uniq_concat(vals):
        seen = set()
        out = []
        for v in vals:
            if pd.isna(v):
                continue
            s = str(v).strip()
            if s in ["", "-"]:
                continue
            if s not in seen:
                seen.add(s)
                out.append(s)
        return ", ".join(out)

    bad_status = {"LOCAL", "PM", "DOWN"}

    # 선택값 계산(계산용)
    df["_eqpid_sel"] = df["사전지정eqp"].combine_first(df["pems_eqpids"]).combine_first(df["eqpid"])
    df["_eqpcham_base"] = df["eqpcham"].combine_first(df["eqpid"])
    df["_eqpcham_sel"] = df["사전지정eqp"].combine_first(df["pems_chamberids"]).combine_first(df["_eqpcham_base"])

    # 그룹 키: 요구대로 (lot_id, stepseq, recipeid)
    gkeys2 = ["lot_id", "stepseq", "recipeid"]

    # 재집계 결과 저장 dict
    EQPGROUP_map = {}
    COMPAT_map = {}
    EQPGROUP_CH_map = {}
    COMPAT_CH_map = {}
    step_issue_map = {}
    compat_issue_map = {}
    equip_issue_pfr1_map = {}
    equip_issue_comp_map = {}
    tip_issue_pfr1_map = {}
    tip_issue_comp_map = {}

    for gk, sub in df.groupby(gkeys2, sort=False):
        # 대체 조건 존재?
        has_override_eqp = sub["사전지정eqp"].notna().any()
        has_override_pems = sub["pems_eqpids"].notna().any() or sub["pems_chamberids"].notna().any()
        need_override = bool(has_override_eqp or has_override_pems)

        # override 기준으로 필터링할 subset
        use = sub
        if need_override:
            # 선택값(그룹 내 동일하다고 가정하지만, 혹시 몰라 대표값 사용)
            eqpid_sel = sub["_eqpid_sel"].dropna()
            eqpcham_sel = sub["_eqpcham_sel"].dropna()
            eqpid_sel = eqpid_sel.iloc[0] if len(eqpid_sel) else None
            eqpcham_sel = eqpcham_sel.iloc[0] if len(eqpcham_sel) else None

            # 매칭 조건:
            # - eqpid가 eqpid_sel과 일치하거나
            # - eqpcham_base(=eqpcham 없으면 eqpid)가 eqpcham_sel과 일치
            base_ch = sub["_eqpcham_base"]
            m1 = (sub["eqpid"] == eqpid_sel) if eqpid_sel is not None else False
            m2 = (base_ch == eqpcham_sel) if eqpcham_sel is not None else False
            mask = m1 | m2

            # 매칭되는 path가 하나도 없으면(데이터 불일치) 원본으로 fallback
            if mask.any():
                use = sub[mask].copy()
            else:
                use = sub

        # group/issue 계산은 use로 수행
        is_pfr1 = (use["eqpline"] == "PFR1")
        prevent_flag = (use["prevent"] == "PREVENT")
        eqpstatus_bad = use["eqpstatus"].isin(list(bad_status))

        # 1~4
        EQPGROUP_map[gk] = uniq_concat(use.loc[is_pfr1, "eqpid"])
        COMPAT_map[gk] = uniq_concat(use.loc[~is_pfr1, "eqpid"])
        EQPGROUP_CH_map[gk] = uniq_concat(use.loc[is_pfr1, "_eqpcham_base"])
        COMPAT_CH_map[gk] = uniq_concat(use.loc[~is_pfr1, "_eqpcham_base"])

        # 5/8 이슈 라벨
        pfr1_total = int(is_pfr1.sum())
        pfr1_issue = int((is_pfr1 & (eqpstatus_bad | prevent_flag)).sum())
        non_total = int((~is_pfr1).sum())
        non_issue = int(((~is_pfr1) & (eqpstatus_bad | prevent_flag)).sum())

        def label(tot, iss):
            if tot > 0 and iss == tot:
                return "ALL ISSUE"
            if tot > 0 and 0 < iss < tot:
                return "일부 ISSUE"
            return ""

        step_issue_map[gk] = label(pfr1_total, pfr1_issue)
        compat_issue_map[gk] = label(non_total, non_issue)

        # 6/9 설비이슈
        def status_issue(sub2, pfr1=True):
            sub3 = sub2[sub2["eqpline"] == "PFR1"] if pfr1 else sub2[sub2["eqpline"] != "PFR1"]
            d = {"LOCAL": set(), "PM": set(), "DOWN": set()}
            for _, r in sub3.iterrows():
                b = r.get("eqpstatus_body")
                c = r.get("eqpstatus_cham")
                if b in bad_status:
                    v = r.get("eqpid")
                    if not pd.isna(v):
                        d[b].add(str(v).strip())
                if c in bad_status:
                    v = r.get("_eqpcham_base")
                    if not pd.isna(v):
                        d[c].add(str(v).strip())
            parts = []
            for s in ["LOCAL", "PM", "DOWN"]:
                items = sorted([x for x in d[s] if x not in ["", "-"]])
                if items:
                    parts.append(f"{s}: {', '.join(items)}")
            return ", ".join(parts) if parts else ""

        equip_issue_pfr1_map[gk] = status_issue(use, pfr1=True)
        equip_issue_comp_map[gk] = status_issue(use, pfr1=False)

        # 7/10 TIP이슈
        def tip_issue(sub2, pfr1=True):
            sub3 = sub2[sub2["eqpline"] == "PFR1"] if pfr1 else sub2[sub2["eqpline"] != "PFR1"]
            sset = set()
            for _, r in sub3.iterrows():
                if r.get("prevent") != "PREVENT":
                    continue
                if r.get("type_body") == "PREVENT":
                    v = r.get("eqpid")
                    if not pd.isna(v):
                        sset.add(str(v).strip())
                if r.get("type_cham") == "PREVENT":
                    v = r.get("_eqpcham_base")
                    if not pd.isna(v):
                        sset.add(str(v).strip())
            items = sorted([x for x in sset if x not in ["", "-"]])
            if not items:
                return ""
            return f"PREVENT: {', '.join(items)}"

        tip_issue_pfr1_map[gk] = tip_issue(use, pfr1=True)
        tip_issue_comp_map[gk] = tip_issue(use, pfr1=False)

    # 재집계 반영(그룹 내 모든 행에 반복)
    idx_map = df.set_index(gkeys2).index
    df["EQPGROUP"] = idx_map.map(EQPGROUP_map).fillna("")
    df["호환등록"] = idx_map.map(COMPAT_map).fillna("")
    df["EQPGROUP(CHAM)"] = idx_map.map(EQPGROUP_CH_map).fillna("")
    df["호환등록(CHAM)"] = idx_map.map(COMPAT_CH_map).fillna("")
    df["스텝이슈"] = idx_map.map(step_issue_map).fillna("")
    df["호환이슈"] = idx_map.map(compat_issue_map).fillna("")
    df["설비이슈"] = idx_map.map(equip_issue_pfr1_map).fillna("")
    df["호환_설비이슈"] = idx_map.map(equip_issue_comp_map).fillna("")
    df["TIP이슈"] = idx_map.map(tip_issue_pfr1_map).fillna("")
    df["호환_TIP이슈"] = idx_map.map(tip_issue_comp_map).fillna("")

    # -------------------------
    # 5) status 대체
    # -------------------------
    df["status"] = np.where(
        (df["status"] == "WAIT") & (df["스텝이슈"] == "ALL ISSUE"),
        "WAIT(진행불가)",
        df["status"]
    )

    # -------------------------
    # 2) 중복 제거: over(lot_id, stepseq, recipeid)에서 1행만 남기기
    # - eqpcham path 단위로 늘어난 행을 1행으로 축약
    # - 어떤 행을 남길지: 현스텝 우선, 그 다음 연속끝 우선, 그 외 첫 행
    # -------------------------
    df["_prio"] = 2
    df.loc[df["stepseq"] == df["현step"], "_prio"] = 0
    df.loc[df["연속"] == "연속끝", "_prio"] = 1
    df = (
        df.sort_values(gkeys2 + ["_prio"], kind="mergesort")
          .drop_duplicates(subset=gkeys2, keep="first")
          .reset_index(drop=True)
    )

    # -------------------------
    # 6) delaytime 표시 규칙: sentinel이면 null
    # -------------------------
    sentinel = {"1000000020", "1000000000"}
    df["delaytime"] = np.where(df["delaytime"].isin(list(sentinel)), np.nan, df["delaytime"])

    # -------------------------
    # 최종 컬럼 선택/순서
    # -------------------------
    final_cols = [
        "lot_id",
        "lot_type",
        "hot_lot_level",
        "cur_qty",
        "carr_id",
        "status",
        "proc_id",
        "layerid",
        "현step",
        "areaname",
        "사전지정",
        "PEMS",
        "적용PEMSNO",
        "stepseq",
        "descript",
        "recipeid",
        "연속",
        "delaytime",
        "n2_delay_time_mins",
        "areaname", # 요청대로 중복 포함(원문 그대로)
        "cur_line_id",
        "eqpline",
        "eqptype",
        "batch_kind",
        "스텝이슈",
        "호환이슈",
        "EQPGROUP",
        "호환등록",
        "EQPGROUP(CHAM)",
        "호환등록(CHAM)",
        "설비이슈",
        "호환_설비이슈",
        "TIP이슈",
        "호환_TIP이슈"
    ]

    # 없는 컬럼은 생성(안전)
    for c in final_cols:
        if c not in df.columns:
            df[c] = np.nan

    # 계산용 컬럼 제거
    drop_tmp = [c for c in df.columns if c.startswith("_")]
    if drop_tmp:
        df.drop(columns=drop_tmp, inplace=True)

    df = df[final_cols].copy()
    return df


# -----RND_PLAN_DEF-----------------------------------------------------------------------------------------------------------------

def build_df_mp_v9(df_mclot: pd.DataFrame, df_path: pd.DataFrame) -> pd.DataFrame:
    """
    df_mp 생성 (lot_level 강제 주입 문제 '원천 차단' 버전)

    이번 버전에서 바뀐 핵심(요구사항 100% 반영):
    - df_mclot의 lot_level 컬럼명이 실제로는 대소문자/공백/형태가 다를 수 있음
      (예: LOT_LEVEL, lot_level , Lot_Level, lotlevel 등)
    - 그래서 "lot_level 소스 컬럼을 자동 탐지"해서 _lot_level_src로 고정 저장
    - 결과 hot_lot_level은 무조건 _lot_level_src 값을 사용
    - df_mclot에 lot_level이 명확히 있는데도 결과가 전부 NULL인 문제는
      대부분 '컬럼명 불일치(대소문자/공백)' 또는 '다른 이름으로 들어온 lot_level'이 원인이라
      이 방식이 가장 현실적인 근본해결임
    - NA boolean ambiguity는 기존처럼 NumPy 마스크로 차단
    """

    # ------------------------------------------------------------
    # 0) 입력 복사 + 내부 컬럼 잔재 제거
    # ------------------------------------------------------------
    p0 = df_path.copy()
    m0 = df_mclot.copy()

    p0 = p0.drop(columns=[c for c in p0.columns if isinstance(c, str) and c.startswith("_")], errors="ignore")
    m0 = m0.drop(columns=[c for c in m0.columns if isinstance(c, str) and c.startswith("_")], errors="ignore")

    # ------------------------------------------------------------
    # 1) df_mclot에서 lot_level "실제 컬럼" 자동 탐지 -> _lot_level_src 생성
    #    - 우선순위: lot_level 계열 → 그 다음 hot_lot_level 계열(혹시 lot_level이 잘못 들어온 경우 대비)
    # ------------------------------------------------------------
    def _canon(name: str) -> str:
        return "".join(str(name).strip().lower().split())

    m_cols = list(m0.columns)
    canon_map = {_canon(c): c for c in m_cols}  # 동일 canon이면 마지막이 덮일 수 있음(현실적으로 큰 문제 없음)

    # 현실에서 자주 보는 후보들(우선순위 순)
    lot_level_candidates = [
        "lot_level", "lotlevel", "lotlvl", "lot_level_id", "lotlevelid",
        "lot_level_no", "lotlevelno",
        "hot_lot_level", "hotlotlevel"  # 혹시 lot_level이 여기 들어온 경우 대비(하지만 최후의 fallback)
    ]
    # 대소문자/공백 제거된 형태로 탐색
    found_lot_level_col = None
    for k in lot_level_candidates:
        ck = _canon(k)
        if ck in canon_map:
            found_lot_level_col = canon_map[ck]
            break

    if found_lot_level_col is None:
        # lot_level을 못 찾으면, 그래도 컬럼은 만들어 두되(전부 NA) 이후 로직은 정상 진행
        m0["_lot_level_src"] = pd.NA
    else:
        m0["_lot_level_src"] = m0[found_lot_level_col]

    # ------------------------------------------------------------
    # 2) 필요 컬럼 보장 (path / mclot)
    # ------------------------------------------------------------
    need_p = [
        "lot_id", "order_seq", "step_seq", "delay_step_type",
        "layer_id", "step_desc", "recipe_id", "delay_time_mins",
        "eqp_type", "eqp_group_id", "tkin_type"
    ]
    need_m = [
        "lot_id", "lot_type",
        "_lot_level_src",         # ★ 여기만 쓰면 됨
        "cur_qty", "carr_id", "status",
        "proc_id", "step_seq", "cur_line_id"
    ]

    for c in need_p:
        if c not in p0.columns:
            p0[c] = pd.NA
    for c in need_m:
        if c not in m0.columns:
            m0[c] = pd.NA

    p = p0[need_p].copy()
    m = m0[need_m].copy()

    # ------------------------------------------------------------
    # 3) step key 정규화(조인 매칭률 최대화)
    # ------------------------------------------------------------
    def _normalize_step_key_str(s: pd.Series) -> pd.Series:
        s_str = s.astype("string").fillna("").str.strip()
        s_num = pd.to_numeric(s_str, errors="coerce")
        # 숫자로 해석 가능하면 정수 문자열(예: 100.0 -> "100"), 아니면 원문
        out = pd.Series(
            np.where(~np.isnan(s_num.to_numpy(dtype=float)),
                     s_num.astype("Int64").astype("string"),
                     s_str),
            index=s.index
        ).astype("string")
        return out.replace({"<NA>": "", "nan": ""})

    p["lot_id"] = p["lot_id"].astype(str)
    m["lot_id"] = m["lot_id"].astype(str)

    p["_stepseq_raw"] = p["step_seq"]
    m["_curstep_raw"] = m["step_seq"]

    p["_step_key"] = _normalize_step_key_str(p["step_seq"])
    m["_step_key"] = _normalize_step_key_str(m["step_seq"])

    # ------------------------------------------------------------
    # 4) path 정렬 + pos
    # ------------------------------------------------------------
    p["_order_seq"] = pd.to_numeric(p["order_seq"], errors="coerce").astype("float64")
    p = p.sort_values(["lot_id", "_order_seq"], kind="mergesort").reset_index(drop=True)
    p["pos"] = p.groupby("lot_id").cumcount().astype("int32")

    # ------------------------------------------------------------
    # 5) df_path에서 S~Y 연속구간 번호(seg_no) 부여 (S 없이 Y만 있어도 구간 인정)
    # ------------------------------------------------------------
    dstep_np = p["delay_step_type"].astype("string").fillna("").to_numpy()
    in_seg_np = np.isin(dstep_np, np.array(["S", "Y"], dtype=object))

    prev_np = (
        pd.Series(in_seg_np, index=p.index)
        .groupby(p["lot_id"])
        .shift(1)
        .fillna(False)
        .to_numpy(dtype=bool)
    )
    seg_start_np = in_seg_np & (~prev_np)

    seg_no = (
        pd.Series(seg_start_np.astype(int), index=p.index)
        .groupby(p["lot_id"])
        .cumsum()
    )
    p["seg_no"] = seg_no.where(in_seg_np, pd.NA).astype("Int64")

    # 구간 끝: 마지막 Y 우선, 없으면 구간 마지막 row
    seg_mask = p["seg_no"].notna()
    if seg_mask.any():
        last_any = p.loc[seg_mask].groupby(["lot_id", "seg_no"])["pos"].max()
        last_y = p.loc[seg_mask & (dstep_np == "Y")].groupby(["lot_id", "seg_no"])["pos"].max()
        seg_end = last_any.copy()
        seg_end.update(last_y)

        end_map = seg_end.to_dict()
        lasty_map = last_y.to_dict()
    else:
        end_map, lasty_map = {}, {}

    keys = list(zip(p["lot_id"].to_numpy(), p["seg_no"].to_numpy()))
    p["seg_end_pos"] = [end_map.get(k, pd.NA) for k in keys]
    p["seg_last_y_pos"] = [lasty_map.get(k, pd.NA) for k in keys]

    # ------------------------------------------------------------
    # 6) m 기준으로 "현step 매칭 1건" left join (cur_pos/cur_seg_no/cur_end_pos)
    # ------------------------------------------------------------
    p_cur = (
        p[["lot_id", "_step_key", "pos", "seg_no", "seg_end_pos"]]
        .replace({"": pd.NA})
        .dropna(subset=["_step_key"])
        .sort_values(["lot_id", "_step_key", "pos"], kind="mergesort")
        .drop_duplicates(["lot_id", "_step_key"], keep="first")
        .rename(columns={"pos": "cur_pos", "seg_no": "cur_seg_no", "seg_end_pos": "cur_end_pos"})
    )
    m1 = m.merge(p_cur, on=["lot_id", "_step_key"], how="left")

    # ------------------------------------------------------------
    # 7) non-seg: 현step 1건만 / seg: 같은 seg_no 확장 후 cur_pos~cur_end_pos만
    # ------------------------------------------------------------
    m_nonseg = m1[m1["cur_seg_no"].isna()].copy()
    m_seg = m1[m1["cur_seg_no"].notna()].copy()

    p_one = (
        p.replace({"": pd.NA})
         .dropna(subset=["_step_key"])
         .sort_values(["lot_id", "_step_key", "pos"], kind="mergesort")
         .drop_duplicates(["lot_id", "_step_key"], keep="first")
    )
    df_nonseg = m_nonseg.merge(p_one, on=["lot_id", "_step_key"], how="left", suffixes=("", "_p"))

    df_seg = m_seg.merge(
        p[p["seg_no"].notna()],
        left_on=["lot_id", "cur_seg_no"],
        right_on=["lot_id", "seg_no"],
        how="left",
        suffixes=("", "_p")
    )

    # nan-safe 범위 필터 (NumPy float 비교)
    pos_np = pd.to_numeric(df_seg["pos"], errors="coerce").to_numpy(dtype=float)
    cur_np = pd.to_numeric(df_seg["cur_pos"], errors="coerce").to_numpy(dtype=float)
    end_np = pd.to_numeric(df_seg["cur_end_pos"], errors="coerce").to_numpy(dtype=float)

    mask = (~np.isnan(pos_np)) & (~np.isnan(cur_np)) & (~np.isnan(end_np)) & (pos_np >= cur_np) & (pos_np <= end_np)
    df_seg = df_seg.loc[mask].copy()

    df = pd.concat([df_nonseg, df_seg], ignore_index=True, sort=False)

    # ------------------------------------------------------------
    # 8) 연속 라벨(연속첫/연속/연속끝)
    # ------------------------------------------------------------
    d = df["delay_step_type"].astype("string").fillna("").to_numpy()
    pos = pd.to_numeric(df["pos"], errors="coerce").to_numpy(dtype=float)
    last = pd.to_numeric(df["seg_last_y_pos"], errors="coerce").to_numpy(dtype=float)

    df["연속"] = np.select(
        [d == "S", (d == "Y") & (~np.isnan(pos)) & (~np.isnan(last)) & (pos == last), d == "Y"],
        ["연속첫", "연속끝", "연속"],
        default=pd.NA
    )

    # ------------------------------------------------------------
    # 9) areaname / PEMS
    # ------------------------------------------------------------
    eqp = df["eqp_type"].astype("string").fillna("").to_numpy()
    grp = df["eqp_group_id"].astype("string").fillna("").to_numpy()
    first = np.array([g[:1] if g else "" for g in grp], dtype=object)

    df["areaname"] = np.select(
        [
            eqp == "MMETAL", eqp == "POVLAY", eqp == "JSORTE",
            first == "P", first == "D", first == "I", first == "M",
            first == "T", first == "W", first == "E", first == "C"
        ],
        ["METAL", "METRO", "IMP", "PHOTO", "DIFF", "IMP", "METRO", "CVD", "CLN", "ETCH", "CMP"],
        default="-"
    )

    df["PEMS"] = np.where(df["tkin_type"].astype("string").fillna("").to_numpy() == "EIN", "O", pd.NA)

    # ------------------------------------------------------------
    # 10) 출력 컬럼 세팅 (핵심: hot_lot_level = _lot_level_src)
    # ------------------------------------------------------------
    df["현step"] = df["_curstep_raw"]
    df["stepseq"] = df["_stepseq_raw"]

    # ★ 무조건 이 값 사용 (df_mclot에서 잡아온 lot_level)
    df["hot_lot_level"] = df["_lot_level_src"]

    df = df.rename(columns={
        "step_desc": "descript",
        "recipe_id": "recipeid",
        "delay_time_mins": "delaytime",
        "eqp_type": "eqptype",
    })

    df_mp = df[[
        "lot_id",
        "lot_type",
        "hot_lot_level",
        "cur_qty",
        "carr_id",
        "status",
        "proc_id",
        "layer_id",
        "현step",
        "areaname",
        "PEMS",
        "stepseq",
        "descript",
        "recipeid",
        "연속",
        "delaytime",
        "cur_line_id",
        "eqptype",
        "eqp_group_id",
    ]].copy()

    return df_mp


def join_eqpgrouplist_to_mp(df_mp: pd.DataFrame, df_eqpgrouplist: pd.DataFrame) -> pd.DataFrame:
    """
    (수정 반영)
    df_mp(as a) 기준으로 df_eqpgrouplist(as b) left join

    - 조인키:
        a.eqp_group_id = b.eqp_group_name

    - b에서 추가되는 컬럼:
        eqp_id
        eqpstatus
        batch_kind
        eqpline

    - 신규 컬럼:
        '호환대수' = max(seq_order_no) over(eqp_group_name) + 1

    - 결과:
        df_mp 행이 eqp_group 내 eqp_id 개수만큼 확장됨
    """

    a = df_mp.copy()
    b = df_eqpgrouplist.copy()

    # --------------------------------------------------
    # 0) 필수 컬럼 보장
    # --------------------------------------------------
    need_a = ["eqp_group_id"]
    need_b = ["eqp_group_name", "seq_order_no", "eqp_id", "eqpstatus", "batch_kind", "eqpline"]

    for c in need_a:
        if c not in a.columns:
            a[c] = pd.NA
    for c in need_b:
        if c not in b.columns:
            b[c] = pd.NA

    # --------------------------------------------------
    # 1) 타입 정규화
    # --------------------------------------------------
    a["eqp_group_id"] = a["eqp_group_id"].astype("string")

    b["eqp_group_name"] = b["eqp_group_name"].astype("string")
    b["eqp_id"] = b["eqp_id"].astype("string")
    b["eqpstatus"] = b["eqpstatus"].astype("string")
    b["batch_kind"] = b["batch_kind"].astype("string")
    b["eqpline"] = b["eqpline"].astype("string")

    b["seq_order_no"] = pd.to_numeric(b["seq_order_no"], errors="coerce")

    # --------------------------------------------------
    # 2) 호환대수 계산 (group별 max(seq_order_no) + 1)
    # --------------------------------------------------
    max_seq = b.groupby("eqp_group_name")["seq_order_no"].transform("max")
    b["호환대수"] = np.where(
        max_seq.isna(),
        pd.NA,
        (max_seq + 1).astype("Int64")
    )

    # --------------------------------------------------
    # 3) 조인에 사용할 컬럼만 선택
    # --------------------------------------------------
    b_join = b[
        ["eqp_group_name", "eqp_id", "eqpstatus", "batch_kind", "eqpline", "호환대수"]
    ].copy()

    # --------------------------------------------------
    # 4) left join (행 확장)
    # --------------------------------------------------
    out = a.merge(
        b_join,
        how="left",
        left_on="eqp_group_id",
        right_on="eqp_group_name",
        suffixes=("", "_b")
    )

    # 보조 조인키 제거
    out = out.drop(columns=["eqp_group_name"], errors="ignore")

    return out


def join_tkinprevent_to_mpe_v4(
    df_mpe: pd.DataFrame,
    df_tkinprevent: pd.DataFrame
) -> pd.DataFrame:
    """
    [정합성 유지 버전]

    a = df_mpe (build_df_mp_v9 + join_eqpgrouplist_to_mp 결과)
    t = df_tkinprevent

    변경사항:
    1) a.eqpstatus -> a.eqpstatus_body 로 rename
    2) add_columns에 아래 컬럼 추가
       - t.eqpstatus_cham
       - t.type_body
       - t.type_cham
    """

    # ------------------------------------------------------------
    # 0) copy
    # ------------------------------------------------------------
    a = df_mpe.copy()
    t = df_tkinprevent.copy()

    # ------------------------------------------------------------
    # 1) 컬럼명 변경 (a.eqpstatus -> eqpstatus_body)
    # ------------------------------------------------------------
    if "eqpstatus" in a.columns and "eqpstatus_body" not in a.columns:
        a = a.rename(columns={"eqpstatus": "eqpstatus_body"})

    # ------------------------------------------------------------
    # 2) 필수 컬럼 보장
    # ------------------------------------------------------------
    need_a = ["recipeid", "stepseq", "proc_id", "eqp_id"]
    need_t = [
        "ppid", "step", "process", "eqpid",
        "eqpcham",
        "batch_kind",
        "eqpstatus_cham",
        "type_body",
        "type_cham",
        "prevent",
        "eqpline",
        "type",
    ]

    for c in need_a:
        if c not in a.columns:
            a[c] = pd.NA
    for c in need_t:
        if c not in t.columns:
            t[c] = pd.NA

    # ------------------------------------------------------------
    # 3) 타입 정규화 (NA-safe)
    # ------------------------------------------------------------
    for c in ["recipeid", "stepseq", "proc_id", "eqp_id"]:
        a[c] = a[c].astype("string")

    for c in ["ppid", "step", "process", "eqpid", "type"]:
        t[c] = t[c].astype("string")

    add_cols = [
        "eqpcham",
        "batch_kind",
        "eqpstatus_cham",
        "type_body",
        "type_cham",
        "prevent",
        "eqpline",
    ]

    for c in add_cols:
        t[c] = t[c].astype("string")
        if c not in a.columns:
            a[c] = pd.NA
        else:
            a[c] = a[c].astype("string")

    # ------------------------------------------------------------
    # STEP 1: 4-key join
    # ------------------------------------------------------------
    t1 = t[["ppid", "step", "process", "eqpid"] + add_cols].drop_duplicates(ignore_index=True)

    b1 = a.merge(
        t1,
        how="left",
        left_on=["recipeid", "stepseq", "proc_id", "eqp_id"],
        right_on=["ppid", "step", "process", "eqpid"],
        suffixes=("", "__t1")
    )

    for c in add_cols:
        c1 = f"{c}__t1"
        if c1 in b1.columns:
            b1[c] = b1[c].where(b1[c].notna(), b1[c1])
            b1 = b1.drop(columns=[c1], errors="ignore")

    b1 = b1.drop(columns=["ppid", "step", "process", "eqpid"], errors="ignore")

    # ------------------------------------------------------------
    # STEP 2: eqpid only + filter
    # ------------------------------------------------------------
    proc_np = t["process"].astype("string").fillna("").to_numpy()
    step_np = t["step"].astype("string").fillna("").to_numpy()
    ppid_np = t["ppid"].astype("string").fillna("").to_numpy()
    type_np = t["type"].astype("string").fillna("").to_numpy()

    filt2 = ((proc_np == "-") | (step_np == "-") | (ppid_np == "-")) & (type_np == "PREVENT")

    t2 = t.loc[filt2, ["eqpid"] + add_cols].drop_duplicates(ignore_index=True)

    b2 = b1.merge(
        t2,
        how="left",
        left_on=["eqp_id"],
        right_on=["eqpid"],
        suffixes=("", "__t2")
    )

    for c in add_cols:
        c2 = f"{c}__t2"
        if c2 in b2.columns:
            b2[c] = b2[c].where(b2[c].notna(), b2[c2])
            b2 = b2.drop(columns=[c2], errors="ignore")

    b2 = b2.drop(columns=["eqpid"], errors="ignore")

    # ------------------------------------------------------------
    # STEP 3 대상 분리 (기존 규칙 유지)
    # ------------------------------------------------------------
    joined_mask = b2[add_cols].notna().any(axis=1)
    done = b2.loc[joined_mask].copy()
    todo = b2.loc[~joined_mask].copy()

    # ------------------------------------------------------------
    # STEP 3: 3-key join
    # ------------------------------------------------------------
    t3 = t[["ppid", "step", "process"] + add_cols].drop_duplicates(ignore_index=True)

    b3 = todo.merge(
        t3,
        how="left",
        left_on=["recipeid", "stepseq", "proc_id"],
        right_on=["ppid", "step", "process"],
        suffixes=("", "__t3")
    )

    for c in add_cols:
        c3 = f"{c}__t3"
        if c3 in b3.columns:
            b3[c] = b3[c].where(b3[c].notna(), b3[c3])
            b3 = b3.drop(columns=[c3], errors="ignore")

    b3 = b3.drop(columns=["ppid", "step", "process"], errors="ignore")

    # ------------------------------------------------------------
    # 최종 결합 + DISTINCT
    # ------------------------------------------------------------
    out = pd.concat([done, b3], ignore_index=True, sort=False)
    out = out.drop_duplicates(ignore_index=True)

    return out



