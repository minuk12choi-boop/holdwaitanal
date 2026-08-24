def add_mpet_issue_columns(df_mpet: pd.DataFrame) -> pd.DataFrame:
    """
    df_mpet(= build_df_mp_v9 -> join_eqpgrouplist_to_mp -> join_tkinprevent_to_mpe_v4 결과)
    사용자 정의 1)~12) 컬럼 추가 + status 값 변경 + 컬럼/정렬 정리

    ✅ 변경사항(요구 반영)
    - eqpline_eqptype 컬럼 생성 로직/컬럼 자체를 제거
    - 최종 컬럼에는 eqpline, eqptype를 '별도 컬럼'으로 유지
    - 나머지 로직은 동일
    """

    a = df_mpet.copy()

    # ------------------------------------------------------------
    # 0) 필수 컬럼 보장
    # ------------------------------------------------------------
    need_cols = [
        "lot_id", "lot_type", "hot_lot_level", "cur_qty", "carr_id", "status", "proc_id",
        "layer_id", "현step", "areaname", "PEMS", "stepseq", "descript", "recipeid",
        "연속", "delaytime", "cur_line_id",
        "eqpline", "eqptype", "batch_kind",
        "eqp_id", "eqpcham",
        "eqpstatus_body", "eqpstatus_cham",
        "type_body", "type_cham",
    ]
    for c in need_cols:
        if c not in a.columns:
            a[c] = pd.NA

    # string 통일(비교 안정)
    str_cols = [
        "lot_id", "lot_type", "hot_lot_level", "carr_id", "status", "proc_id",
        "layer_id", "현step", "areaname", "PEMS", "stepseq", "descript", "recipeid",
        "연속", "cur_line_id",
        "eqpline", "eqptype", "batch_kind",
        "eqp_id", "eqpcham",
        "eqpstatus_body", "eqpstatus_cham",
        "type_body", "type_cham",
    ]
    for c in str_cols:
        a[c] = a[c].astype("string")

    # ------------------------------------------------------------
    # 1) 정렬키 생성(집계 등장순서 고정)
    # ------------------------------------------------------------
    a["_stepseq_sort"] = pd.to_numeric(a["stepseq"].astype("string").str.strip(), errors="coerce")
    a["_eqp_id_sort"] = a["eqp_id"].fillna("")
    a["_eqpcham_sort"] = a["eqpcham"].fillna("")

    a = a.sort_values(
        ["lot_id", "_stepseq_sort", "stepseq", "_eqp_id_sort", "_eqpcham_sort"],
        kind="mergesort"
    ).reset_index(drop=True)

    grp_keys = ["lot_id", "stepseq"]

    # ------------------------------------------------------------
    # 2) pd.NA-safe getter (pd.NA를 bool로 평가하지 않음)
    # ------------------------------------------------------------
    def _get_str(row, col: str) -> str:
        v = row.get(col, "")
        if pd.isna(v):
            return ""
        return str(v)

    # ------------------------------------------------------------
    # 3) helper: uniqueconcatenate (중복 제거 + 등장순서 유지)
    # ------------------------------------------------------------
    def unique_concat(series: pd.Series) -> Optional[str]:
        vals = series.dropna()
        if vals.empty:
            return None
        seen = set()
        out: list[str] = []
        for v in vals.astype("string").tolist():
            v2 = str(v).strip()
            if not v2 or v2.lower() == "nan" or v2 == "<NA>":
                continue
            if v2 not in seen:
                seen.add(v2)
                out.append(v2)
        return ", ".join(out) if out else None

    # ------------------------------------------------------------
    # 4) PFR1 여부 (numpy bool)
    # ------------------------------------------------------------
    eqpline_arr = a["eqpline"].fillna("").to_numpy(dtype=object)
    is_pfr1_np = (eqpline_arr == "PFR1")  # numpy bool

    # ------------------------------------------------------------
    # 1) EQPGROUP (PFR1): uniqueconcatenate(eqpcham)
    # ------------------------------------------------------------
    eqpgroup_map = (
        a.loc[is_pfr1_np, grp_keys + ["eqpcham"]]
        .groupby(grp_keys, sort=False)["eqpcham"]
        .apply(unique_concat)
        .to_dict()
    )
    a["EQPGROUP"] = [
        eqpgroup_map.get((lid, ss), None) if pfr1 else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    # ------------------------------------------------------------
    # 3) 호환등록 (not PFR1): uniqueconcatenate(eqpcham)
    # ------------------------------------------------------------
    compatreg_map = (
        a.loc[~is_pfr1_np, grp_keys + ["eqpcham"]]
        .groupby(grp_keys, sort=False)["eqpcham"]
        .apply(unique_concat)
        .to_dict()
    )
    a["호환등록"] = [
        compatreg_map.get((lid, ss), None) if (not pfr1) else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    # ------------------------------------------------------------
    # 2) EQPGROUP(CHAM), 4) 호환등록(CHAM)
    # ------------------------------------------------------------
    forced_kinds = {"SINGLE_METROLOGY", "BATCH_FURNACE", "BATCH_WET"}
    force_eqpid_map = (
        a.assign(_force=a["batch_kind"].fillna("").isin(list(forced_kinds)).to_numpy(dtype=bool))
        .groupby(grp_keys, sort=False)["_force"]
        .max()
        .to_dict()
    )

    def build_cham_concat_for_group(df_g: pd.DataFrame) -> Optional[str]:
        if df_g.empty:
            return None
        lid = str(df_g["lot_id"].iloc[0])
        ss = str(df_g["stepseq"].iloc[0])
        force = bool(force_eqpid_map.get((lid, ss), False))
        return unique_concat(df_g["eqp_id"] if force else df_g["eqpcham"])

    eqpgroup_cham_map = (
        a.loc[is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(build_cham_concat_for_group)
        .to_dict()
    )
    a["EQPGROUP(CHAM)"] = [
        eqpgroup_cham_map.get((lid, ss), None) if pfr1 else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    compatreg_cham_map = (
        a.loc[~is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(build_cham_concat_for_group)
        .to_dict()
    )
    a["호환등록(CHAM)"] = [
        compatreg_cham_map.get((lid, ss), None) if (not pfr1) else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    # ------------------------------------------------------------
    # 5/6) 설비이슈 / 호환_설비이슈
    # ------------------------------------------------------------
    ISSUE_STAT = {"LOCAL", "PM", "DOWN"}

    def build_status_issue_text(df_g: pd.DataFrame) -> Optional[str]:
        if df_g.empty:
            return None

        buckets: dict[str, list[str]] = {"LOCAL": [], "PM": [], "DOWN": []}
        seen: dict[str, set[str]] = {"LOCAL": set(), "PM": set(), "DOWN": set()}

        for _, r in df_g.iterrows():
            cs = _get_str(r, "eqpstatus_cham")
            bs = _get_str(r, "eqpstatus_body")
            cham = _get_str(r, "eqpcham").strip()
            eqpid = _get_str(r, "eqp_id").strip()

            if cs in ISSUE_STAT and cham and cham.lower() != "nan" and cham != "<NA>":
                if cham not in seen[cs]:
                    seen[cs].add(cham)
                    buckets[cs].append(cham)

            if bs in ISSUE_STAT and eqpid and eqpid.lower() != "nan" and eqpid != "<NA>":
                if eqpid not in seen[bs]:
                    seen[bs].add(eqpid)
                    buckets[bs].append(eqpid)

        parts: list[str] = []
        for st in ["LOCAL", "PM", "DOWN"]:
            if buckets[st]:
                parts.append(f"{st}: {', '.join(buckets[st])}")
        return ", ".join(parts) if parts else None

    equip_issue_map = (
        a.loc[is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(build_status_issue_text)
        .to_dict()
    )
    a["설비이슈"] = [
        equip_issue_map.get((lid, ss), None) if pfr1 else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    compat_equip_issue_map = (
        a.loc[~is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(build_status_issue_text)
        .to_dict()
    )
    a["호환_설비이슈"] = [
        compat_equip_issue_map.get((lid, ss), None) if (not pfr1) else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    # ------------------------------------------------------------
    # 7/8) TIP이슈 / 호환_TIP이슈
    # ------------------------------------------------------------
    def build_tip_text(df_g: pd.DataFrame) -> Optional[str]:
        if df_g.empty:
            return None

        items: list[str] = []
        seen: set[str] = set()

        for _, r in df_g.iterrows():
            tc = _get_str(r, "type_cham")
            tb = _get_str(r, "type_body")
            cham = _get_str(r, "eqpcham").strip()
            eqpid = _get_str(r, "eqp_id").strip()

            if tc == "PREVENT" and cham and cham.lower() != "nan" and cham != "<NA>":
                if cham not in seen:
                    seen.add(cham)
                    items.append(cham)

            if tb == "PREVENT" and eqpid and eqpid.lower() != "nan" and eqpid != "<NA>":
                if eqpid not in seen:
                    seen.add(eqpid)
                    items.append(eqpid)

        return f"PREVENT: {', '.join(items)}" if items else None

    tip_map = (
        a.loc[is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(build_tip_text)
        .to_dict()
    )
    a["TIP이슈"] = [
        tip_map.get((lid, ss), None) if pfr1 else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    compat_tip_map = (
        a.loc[~is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(build_tip_text)
        .to_dict()
    )
    a["호환_TIP이슈"] = [
        compat_tip_map.get((lid, ss), None) if (not pfr1) else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    # ------------------------------------------------------------
    # 9/10) 스텝이슈 / 호환이슈 (numpy bool)
    # ------------------------------------------------------------
    eqpstatus_body_arr = a["eqpstatus_body"].fillna("").to_numpy(dtype=object)
    eqpstatus_cham_arr = a["eqpstatus_cham"].fillna("").to_numpy(dtype=object)
    type_body_arr = a["type_body"].fillna("").to_numpy(dtype=object)
    type_cham_arr = a["type_cham"].fillna("").to_numpy(dtype=object)

    issue_stat_np = np.isin(eqpstatus_body_arr, list(ISSUE_STAT)) | np.isin(eqpstatus_cham_arr, list(ISSUE_STAT))
    tip_np = (type_body_arr == "PREVENT") | (type_cham_arr == "PREVENT")
    blocked_np = (issue_stat_np | tip_np).astype(bool)

    a["_blocked"] = blocked_np.astype(np.int8)

    def step_issue_label(df_g: pd.DataFrame) -> Optional[str]:
        if df_g.empty:
            return None
        path_cnt = int(len(df_g))
        blk_cnt = int(pd.to_numeric(df_g["_blocked"], errors="coerce").fillna(0).sum())
        if blk_cnt <= 0:
            return None
        return "ALL ISSUE" if path_cnt <= blk_cnt else "일부 ISSUE"

    pfr1_issue_map = (
        a.loc[is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(step_issue_label)
        .to_dict()
    )
    non_issue_map = (
        a.loc[~is_pfr1_np, :]
        .groupby(grp_keys, sort=False)
        .apply(step_issue_label)
        .to_dict()
    )

    a["스텝이슈"] = [
        pfr1_issue_map.get((lid, ss), None) if pfr1 else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]
    a["호환이슈"] = [
        non_issue_map.get((lid, ss), None) if (not pfr1) else None
        for lid, ss, pfr1 in zip(a["lot_id"].tolist(), a["stepseq"].tolist(), is_pfr1_np.tolist())
    ]

    # ------------------------------------------------------------
    # 11) status 변경 (numpy 인덱싱)
    # ------------------------------------------------------------
    step_issue_arr = pd.Series(a["스텝이슈"], dtype="object").fillna("").to_numpy(dtype=object)
    compat_issue_arr = pd.Series(a["호환이슈"], dtype="object").fillna("").to_numpy(dtype=object)
    status_arr_cmp = pd.Series(a["status"], dtype="object").fillna("").to_numpy(dtype=object)

    all_issue_np = (step_issue_arr == "ALL ISSUE") | (compat_issue_arr == "ALL ISSUE")
    wait_np = (status_arr_cmp == "WAIT")
    change_mask = (all_issue_np & wait_np)

    status_new = pd.Series(a["status"], dtype="object").to_numpy(dtype=object)  # 원본 NA 유지
    status_new[change_mask] = "WAIT(진행불가)"
    a["status"] = status_new

    # layerid 컬럼명 맞춤
    if "layerid" not in a.columns:
        a["layerid"] = a["layer_id"]

    # 최종 정렬
    a = a.sort_values(
        ["lot_id", "_stepseq_sort", "stepseq", "eqp_id", "eqpcham"],
        kind="mergesort"
    ).reset_index(drop=True)

    # ✅ 최종 컬럼: eqpline, eqptype를 별도 컬럼으로 유지 (eqpline_eqptype 제거)
    final_cols = [
        "lot_id", "lot_type", "hot_lot_level", "cur_qty", "carr_id", "status", "proc_id",
        "layerid", "현step", "areaname", "PEMS", "stepseq", "descript", "recipeid", "연속", "delaytime",
        "cur_line_id", "eqpline", "eqptype", "batch_kind",
        "스텝이슈", "호환이슈",
        "EQPGROUP", "호환등록", "EQPGROUP(CHAM)", "호환등록(CHAM)",
        "설비이슈", "호환_설비이슈", "TIP이슈", "호환_TIP이슈"
    ]
    for c in final_cols:
        if c not in a.columns:
            a[c] = pd.NA

    out = a[final_cols].copy()
    return out


def merge_fab_rnd_append_rows(
    df_fab_plan: pd.DataFrame,
    df_rnd_plan: pd.DataFrame,
    source_col: str = "plan_source",
) -> pd.DataFrame:
    """
    fab_plan / rnd_plan 결과 DF를 '밑으로 붙이는 방식'(UNION ALL)으로 병합.

    요구사항 반영:
    - 공통 컬럼명은 그대로 정렬/정합
    - 서로 없는 컬럼은 그대로 유지(합집합 컬럼)
    - 출처 컬럼(source_col) 추가:
        FAB_PLAN, RND_PLAN

    반환:
    - 컬럼 = (df_fab_plan ∪ df_rnd_plan) + source_col
    - 행 = df_fab_plan rows + df_rnd_plan rows
    """

    fab = df_fab_plan.copy()
    rnd = df_rnd_plan.copy()

    # 출처 컬럼 추가
    fab[source_col] = "FAB_PLAN"
    rnd[source_col] = "RND_PLAN"

    # 컬럼 합집합 구성 (fab 컬럼 순서 유지 + rnd에만 있는 컬럼 뒤에 추가)
    union_cols = list(fab.columns) + [c for c in rnd.columns if c not in fab.columns]

    # 누락 컬럼 자동 생성 (pandas가 결측으로 채움)
    fab = fab.reindex(columns=union_cols)
    rnd = rnd.reindex(columns=union_cols)

    # 밑으로 붙이기
    out = pd.concat([fab, rnd], axis=0, ignore_index=True, sort=False)

    return out

def join_mc_grade_to_all(
    df_all: pd.DataFrame,
    df_mc: pd.DataFrame,
    df_grade: pd.DataFrame
) -> pd.DataFrame:
    """
    df_all(as a) 기준으로
    - df_mc(as m)
    - df_grade(as g)
    LEFT OUTER JOIN

    add columns:
      - m.last_event_date
      - g.grade
      - 경과시간[시] (현재시각 - last_event_date, hour 단위)
    """

    a = df_all.copy()
    m = df_mc.copy()
    g = df_grade.copy()

    # -----------------------------
    # 필수 컬럼 보장
    # -----------------------------
    for c in ["lot_id"]:
        if c not in a.columns:
            raise ValueError("df_all에 lot_id 컬럼이 없습니다.")
        if c not in m.columns:
            m[c] = pd.NA
        if c not in g.columns:
            g[c] = pd.NA

    if "last_event_date" not in m.columns:
        m["last_event_date"] = pd.NA

    if "grade" not in g.columns:
        g["grade"] = pd.NA

    # -----------------------------
    # 1) a LEFT JOIN m
    # -----------------------------
    out = a.merge(
        m[["lot_id", "last_event_date"]],
        how="left",
        on="lot_id"
    )

    # -----------------------------
    # 2) out LEFT JOIN g
    # -----------------------------
    out = out.merge(
        g[["lot_id", "grade"]],
        how="left",
        on="lot_id"
    )

    # -----------------------------
    # 3) 경과시간[시] 계산
    # -----------------------------
    # sysdate = 현재 시각
    now = pd.Timestamp.now()

    out["last_event_date"] = pd.to_datetime(
        out["last_event_date"],
        errors="coerce"
    )

    out["경과시간[시]"] = (
        (now - out["last_event_date"])
        .dt.total_seconds()
        .div(3600)
    )

    # last_event_date가 NULL면 경과시간도 NULL
    out.loc[out["last_event_date"].isna(), "경과시간[시]"] = pd.NA

    return out


def join_issue_to_all_no_row_increase(
    df_all: pd.DataFrame,
    df_issue: pd.DataFrame,
    now: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """
    요구사항 그대로:
    - a=df_all 기준으로 b=df_issue LEFT OUTER JOIN
      join: a.lot_id = b.lotid AND a.stepseq = b.issuestep
    - df_issue 때문에 a의 행수가 늘어나면 안 됨  → df_issue를 (lotid, issuestep) 단위로 피벗/집계해서 붙임
    - add columns (lotid window 개념을 step 단위 집계로 구현):
        HOLDUSER/FTPUSER/EXCEPTIONUSER (uniqueconcatenate)
        HOLDCODE/FTPCODE/EXCEPTIONCODE
        HOLDCMT/FTPCMT/EXCEPTIONCMT
        HOLDTIME/FTPTIME/EXCEPTIONTIME  (각 타입별 min issuetime)
    - 신규 계산 컬럼:
        HOLD/FTP/EXCEPTION : 해당 타입 존재하면 'O' else NA
        ISSUE경과시간[시] : now() - MIN(issuetime) OVER (PARTITION BY lotid)  (hour 단위)
          → lotid 단위 최소 issuetime을 계산해서 a의 모든 해당 lot 행에 붙임
    """

    a = df_all.copy()
    b = df_issue.copy()

    # -----------------------------
    # now() 고정(테스트/재현 가능)
    # -----------------------------
    if now is None:
        now = pd.Timestamp.now()

    # -----------------------------
    # 키/필수컬럼 보장
    # -----------------------------
    for c in ["lot_id", "stepseq"]:
        if c not in a.columns:
            raise ValueError(f"df_all에 '{c}' 컬럼이 없습니다.")
    for c in ["lotid", "issuestep", "issuetype", "issueuser", "issuecode", "issuecmt", "issuetime"]:
        if c not in b.columns:
            b[c] = pd.NA

    # -----------------------------
    # step 키 정규화 (숫자/문자 혼재 대응)
    # - "100.0" -> "100"
    # - 공백 제거
    # -----------------------------
    def _normalize_step_key_str(s: pd.Series) -> pd.Series:
        s_str = s.astype("string").fillna("").str.strip()
        s_num = pd.to_numeric(s_str, errors="coerce")
        out = pd.Series(
            np.where(~np.isnan(s_num.to_numpy(dtype=float)),
                     s_num.astype("Int64").astype("string"),
                     s_str),
            index=s.index
        ).astype("string")
        return out.replace({"<NA>": "", "nan": ""})

    a["_lot_key"] = a["lot_id"].astype("string").fillna("").str.strip()
    a["_step_key"] = _normalize_step_key_str(a["stepseq"])

    b["_lot_key"] = b["lotid"].astype("string").fillna("").str.strip()
    b["_step_key"] = _normalize_step_key_str(b["issuestep"])

    # issuetype / user / code / cmt는 string
    for c in ["issuetype", "issueuser", "issuecode", "issuecmt"]:
        b[c] = b[c].astype("string")

    # issuetime datetime
    b["issuetime"] = pd.to_datetime(b["issuetime"], errors="coerce")

    # -----------------------------
    # uniqueconcatenate (중복 제거 + 등장순서 유지)
    # -----------------------------
    def unique_concat(series: pd.Series) -> Optional[str]:
        vals = series.dropna()
        if vals.empty:
            return None
        seen = set()
        out = []
        for v in vals.astype("string").tolist():
            v2 = str(v).strip()
            if not v2 or v2.lower() == "nan" or v2 == "<NA>":
                continue
            if v2 not in seen:
                seen.add(v2)
                out.append(v2)
        return ", ".join(out) if out else None

    # -----------------------------
    # 1) (lotid, issuestep, issuetype) 단위로 집계
    # -----------------------------
    grp3 = ["_lot_key", "_step_key", "issuetype"]
    agg3 = (
        b.groupby(grp3, sort=False)
         .agg(
            _users=("issueuser", unique_concat),
            _codes=("issuecode", unique_concat),
            _cmts=("issuecmt", unique_concat),
            _mintime=("issuetime", "min"),
            _cnt=("issuetype", "size"),
         )
         .reset_index()
    )

    # 관심 타입만(요구된 3종)
    want_types = ["HOLD", "FTP", "EXCEPTION"]
    agg3 = agg3[agg3["issuetype"].isin(want_types)].copy()

    # -----------------------------
    # 2) 타입별 컬럼으로 피벗 (행 증가 없이 a에 붙일 수 있게)
    # -----------------------------
    # users
    p_users = agg3.pivot_table(
        index=["_lot_key", "_step_key"],
        columns="issuetype",
        values="_users",
        aggfunc="first"
    )
    p_codes = agg3.pivot_table(
        index=["_lot_key", "_step_key"],
        columns="issuetype",
        values="_codes",
        aggfunc="first"
    )
    p_cmts = agg3.pivot_table(
        index=["_lot_key", "_step_key"],
        columns="issuetype",
        values="_cmts",
        aggfunc="first"
    )
    p_time = agg3.pivot_table(
        index=["_lot_key", "_step_key"],
        columns="issuetype",
        values="_mintime",
        aggfunc="first"
    )
    p_cnt = agg3.pivot_table(
        index=["_lot_key", "_step_key"],
        columns="issuetype",
        values="_cnt",
        aggfunc="first"
    )

    # 컬럼명 매핑
    def _rename_pivot(p: pd.DataFrame, suffix: str) -> pd.DataFrame:
        p2 = p.copy()
        p2.columns = [f"{t}{suffix}" for t in p2.columns]
        return p2

    wide = pd.concat(
        [
            _rename_pivot(p_users, "USER"),
            _rename_pivot(p_codes, "CODE"),
            _rename_pivot(p_cmts, "CMT"),
            _rename_pivot(p_time, "TIME"),
            p_cnt.rename(columns={t: f"{t}__CNT" for t in p_cnt.columns}),
        ],
        axis=1
    ).reset_index()

    # 최종 요구 컬럼명으로 변경
    rename_final = {
        "HOLDUSER": "HOLDUSER",
        "FTPUSER": "FTPUSER",
        "EXCEPTIONUSER": "EXCEPTIONUSER",
        "HOLDCODE": "HOLDCODE",
        "FTPCODE": "FTPCODE",
        "EXCEPTIONCODE": "EXCEPTIONCODE",
        "HOLDCMT": "HOLDCMT",
        "FTPCMT": "FTPCMT",
        "EXCEPTIONCMT": "EXCEPTIONCMT",
        "HOLDTIME": "HOLDTIME",
        "FTPTIME": "FTPTIME",
        "EXCEPTIONTIME": "EXCEPTIONTIME",
    }

    # 현재 wide는 예: "HOLDUSER" 형태가 이미 맞음 (HOLD + USER)
    # 다만 안전하게 존재하지 않는 컬럼 생성
    for t in want_types:
        for suf in ["USER", "CODE", "CMT", "TIME", "__CNT"]:
            col = f"{t}{suf}"
            if col not in wide.columns:
                wide[col] = pd.NA

    # HOLD/FTP/EXCEPTION 플래그('O')
    for t in want_types:
        cnt_col = f"{t}__CNT"
        flag_col = t  # 요구: HOLD, FTP, EXCEPTION
        # cnt가 있으면 O, 없으면 NA
        cnt_np = pd.to_numeric(wide[cnt_col], errors="coerce").to_numpy(dtype=float)
        wide[flag_col] = np.where(~np.isnan(cnt_np) & (cnt_np > 0), "O", pd.NA)

    # 필요 없는 cnt 컬럼 제거
    wide = wide.drop(columns=[f"{t}__CNT" for t in want_types], errors="ignore")

    # -----------------------------
    # 3) lotid 단위 ISSUE경과시간[시] (now - min(issuetime) over lotid)
    # -----------------------------
    lot_min_time = (
        b.groupby("_lot_key", sort=False)["issuetime"]
         .min()
         .reset_index()
         .rename(columns={"issuetime": "_issue_min_time"})
    )
    lot_min_time["_issue_elapsed_hours"] = (
        (now - lot_min_time["_issue_min_time"])
        .dt.total_seconds()
        .div(3600)
    )
    lot_min_time.loc[lot_min_time["_issue_min_time"].isna(), "_issue_elapsed_hours"] = pd.NA

    # -----------------------------
    # 4) a LEFT JOIN (lot, step) wide  → 행 증가 없음
    # -----------------------------
    out = a.merge(
        wide,
        how="left",
        left_on=["_lot_key", "_step_key"],
        right_on=["_lot_key", "_step_key"]
    )

    # -----------------------------
    # 5) a LEFT JOIN (lot) elapsed  → 행 증가 없음
    # -----------------------------
    out = out.merge(
        lot_min_time[["_lot_key", "_issue_min_time", "_issue_elapsed_hours"]],
        how="left",
        on="_lot_key"
    )

    out = out.rename(columns={
        "_issue_elapsed_hours": "ISSUE경과시간[시]"
    })

    # 내부키 제거
    out = out.drop(columns=["_lot_key", "_step_key"], errors="ignore")

    # (선택) 컬럼이 아예 없을 수 있으니 보장
    must = [
        "HOLDUSER", "FTPUSER", "EXCEPTIONUSER",
        "HOLDCODE", "FTPCODE", "EXCEPTIONCODE",
        "HOLDCMT", "FTPCMT", "EXCEPTIONCMT",
        "HOLDTIME", "FTPTIME", "EXCEPTIONTIME",
        "HOLD", "FTP", "EXCEPTION",
        "ISSUE경과시간[시]"
    ]
    for c in must:
        if c not in out.columns:
            out[c] = pd.NA

    return out

def apply_wait_block_by_issue(df: pd.DataFrame) -> pd.DataFrame:
    """
    기능:
    1) status == 'WAIT' 이고 (FTP == 'O' 또는 EXCEPTION == 'O') 이면
       status를 'WAIT(진행불가)'로 변경

    2) 연속공정 lot 단위 status 통일
       - 한 lot에서 (현step == stepseq) 인 행(=대표행)의 status가
         'RUN' / 'HOLD' / 'WAIT(진행불가)' 중 하나면
         그 lot의 모든 status를 해당 값으로 통일
       - 대표행 status가 'WAIT'인데,
         같은 lot의 다른 행들 중 하나라도 'WAIT(진행불가)'가 있으면
         그 lot의 모든 status를 'WAIT(진행불가)'로 통일

    주의:
    - 행 증감 없음
    - 기존 컬럼 유지
    - NA 안전 처리 포함
    """

    df = df.copy()

    required_cols = ["lot_id", "status", "FTP", "EXCEPTION", "현step", "stepseq"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"필수 컬럼 누락: {col}")

    # ------------------------------------------------------------
    # 1) WAIT + (FTP/EXCEPTION O) => WAIT(진행불가)
    # ------------------------------------------------------------
    mask_block = (
        (df["status"].fillna("") == "WAIT") &
        (
            (df["FTP"].fillna("") == "O") |
            (df["EXCEPTION"].fillna("") == "O")
        )
    )
    df.loc[mask_block, "status"] = "WAIT(진행불가)"

    # ------------------------------------------------------------
    # 2) 연속공정 lot 단위 status 통일
    #    - 대표행: (현step == stepseq)
    # ------------------------------------------------------------
    lot = df["lot_id"].astype("string").fillna("")

    # 비교키(현step/stepseq)는 문자열 기준으로 통일해서 비교 (NA 안전)
    cur = df["현step"].astype("string").fillna("").str.strip()
    seq = df["stepseq"].astype("string").fillna("").str.strip()

    is_anchor = (cur != "") & (seq != "") & (cur == seq)

    # 대표행 status (lot 당 1개가 정상 가정)
    # lot별 대표행이 여러개면, "첫 번째"를 사용(정렬은 df 현재 순서 기준)
    anchor_df = df.loc[is_anchor, ["lot_id", "status"]].copy()
    anchor_df["lot_id"] = anchor_df["lot_id"].astype("string").fillna("")
    anchor_df["status"] = anchor_df["status"].fillna("")

    anchor_status_map = (
        anchor_df.drop_duplicates(subset=["lot_id"], keep="first")
                 .set_index("lot_id")["status"]
                 .to_dict()
    )

    anchor_status = lot.map(anchor_status_map).fillna("")

    # (A) 대표행 status가 RUN/HOLD/WAIT(진행불가) 이면 lot 전체를 그 값으로 통일
    force_values = {"RUN", "HOLD", "WAIT(진행불가)"}
    mask_force = anchor_status.isin(list(force_values))

    df.loc[mask_force, "status"] = anchor_status.loc[mask_force].to_numpy()

    # (B) 대표행 status가 WAIT인데, lot 내에 WAIT(진행불가) 행이 하나라도 있으면 lot 전체 진행불가
    #     ※ (A)에서 이미 강제된 lot은 제외하고 처리
    status_now = df["status"].fillna("")
    has_block_in_lot = (
        (status_now == "WAIT(진행불가)")
        .groupby(lot)
        .transform("any")
    )

    mask_wait_anchor = (anchor_status == "WAIT")
    mask_force_block = (~mask_force) & mask_wait_anchor & has_block_in_lot

    df.loc[mask_force_block, "status"] = "WAIT(진행불가)"

    return df


