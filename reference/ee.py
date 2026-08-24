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
