from difflib import SequenceMatcher
import re
import pandas as pd


def _cancelled_mask(df: pd.DataFrame) -> pd.Series:
    """폐강일자가 비어 있지 않은 행을 폐강으로 판별합니다."""
    return df["폐강일자"].notna() & df["폐강일자"].astype(str).str.strip().ne("")


def _extract_college(df: pd.DataFrame) -> pd.Series:
    """개설학과를 '>>'로 나눈 첫 부분(단과대)을 추출합니다."""
    return df["개설학과"].fillna("").astype(str).str.split(">>").str[0].str.strip()


def _apply_optional_filter(df: pd.DataFrame, college: str = None, course_type: str = None) -> tuple[pd.DataFrame, str]:
    """질문에서 특정 단과대나 이수구분이 지정된 경우 해당 행만 걸러냅니다."""
    work = df
    notes = []
    if college:
        c_str = str(college).strip()
        if c_str:
            work = work[_extract_college(work) == c_str]
            notes.append(f"단과대='{c_str}'")
    if course_type:
        ct_str = str(course_type).strip()
        if ct_str:
            work = work[work["이수구분"].fillna("").astype(str).str.strip() == ct_str]
            notes.append(f"이수구분='{ct_str}'")
    return work, ", ".join(notes)


def quality_report(df: pd.DataFrame, **kwargs) -> dict:
    """도구 1: 빈칸 개수(열별), 비정상 숫자, 비슷한 값 후보를 점검합니다."""
    rows = []

    for col in df.columns:
        s = df[col]
        if pd.api.types.is_numeric_dtype(s):
            cnt = int(s.isna().sum())
        else:
            cnt = int((s.isna() | s.astype(str).str.strip().eq("")).sum())
        if cnt > 0:
            note = "정상 개설 강좌는 해당 없음" if col in ("폐강일자", "폐강사유") else "누락 또는 해당 없음(파일만으로 구분 불가)"
            rows.append({
                "점검항목": "빈칸",
                "대상": col,
                "건수": cnt,
                "세부내용": note,
            })

    numeric_cols = [
        "학점", "이론", "실습",
        "신청_전", "신청_1", "신청_2", "신청_3", "신청_4",
        "제한_전", "제한_1", "제한_2", "제한_3", "제한_4",
    ]
    for col in numeric_cols:
        if col not in df.columns:
            continue
        neg_mask = df[col].notna() & (df[col] < 0)
        if neg_mask.any():
            excel_rows = [int(i) + 3 for i in df.index[neg_mask]]
            rows.append({
                "점검항목": "음수 값",
                "대상": col,
                "건수": len(excel_rows),
                "세부내용": f"엑셀 행 위치: {excel_rows[:10]}",
            })

    for col in ["이수구분", "대표교수직급", "폐강사유"]:
        vc = df[col].dropna().astype(str).str.strip()
        vc = vc[vc.ne("")].value_counts()
        vals = list(vc.index)
        seen = set()
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                a, b = vals[i], vals[j]
                a_ns, b_ns = a.replace(" ", ""), b.replace(" ", "")
                ratio = SequenceMatcher(None, a_ns, b_ns).ratio()
                is_space = a_ns == b_ns
                is_sim = len(a_ns) >= 4 and len(b_ns) >= 4 and (a_ns in b_ns or b_ns in a_ns or ratio >= 0.72)
                if is_space or is_sim:
                    key = tuple(sorted([a, b]))
                    if key not in seen:
                        seen.add(key)
                        rows.append({
                            "점검항목": "비슷한 값 후보",
                            "대상": col,
                            "건수": int(vc[a] + vc[b]),
                            "세부내용": f"'{a}'({vc[a]}건) vs '{b}'({vc[b]}건)",
                        })

    return {
        "tool": "quality_report",
        "title": "데이터 품질 점검 (빈칸·음수·비슷한 값)",
        "definition": "전체 행을 대상으로 열별 빈칸, 숫자 열의 음수, 분류 열의 비슷한 표기 후보를 점검",
        "used_rows": int(len(df)),
        "excluded_rows": 0,
        "excluded_reason": "없음 (전체 행을 대상으로 점검)",
        "used_columns": list(df.columns),
        "unit": "건",
        "notes": [
            "음수나 빈칸은 원본을 임의로 수정하지 않고 그대로 보고합니다.",
            "폐강일자·폐강사유 빈칸은 정상 개설 강좌('해당 없음')입니다.",
        ],
        "rows": rows,
        "chart": {
            "type": "bar",
            "label": "점검 항목별 건수",
            "labels": [f"{r['점검항목']}:{r['대상']}" for r in rows],
            "values": [r["건수"] for r in rows],
        },
    }


def group_rate(df: pd.DataFrame, group_by: str = "단과대", **kwargs) -> dict:
    """도구 2: 그룹(단과대/이수구분/대상학년)별 행 수·폐강 수·폐강률을 계산합니다."""
    allowed = {"단과대", "이수구분", "대상학년"}
    if group_by not in allowed:
        group_by = "단과대"

    work = df.copy()
    if group_by == "단과대":
        work["_group"] = _extract_college(work)
        used_cols = ["개설학과", "폐강일자"]
    else:
        work["_group"] = work[group_by].fillna("").astype(str).str.strip()
        used_cols = [group_by, "폐강일자"]

    valid_mask = work["_group"].ne("")
    excluded_rows = int((~valid_mask).sum())
    work = work[valid_mask].copy()
    work["_cancelled"] = _cancelled_mask(work).astype(int)

    grouped = (
        work.groupby("_group", as_index=False)
        .agg(행수=("_cancelled", "count"), 폐강수=("_cancelled", "sum"))
    )
    grouped["정상수"] = grouped["행수"] - grouped["폐강수"]
    grouped["폐강률(%)"] = (grouped["폐강수"] / grouped["행수"] * 100).round(2)
    grouped = grouped.sort_values(by=["폐강률(%)", "행수"], ascending=[False, False]).reset_index(drop=True)
    grouped = grouped.rename(columns={"_group": group_by})

    rows = grouped.to_dict(orient="records")
    return {
        "tool": "group_rate",
        "title": f"{group_by}별 개설·폐강률",
        "definition": f"한 행을 분반 1건으로 보고, {group_by}별 전체 행 수 중 폐강(폐강일자가 비어 있지 않은 행) 비율 계산",
        "used_rows": int(len(work)),
        "excluded_rows": excluded_rows,
        "excluded_reason": f"{group_by} 값이 비어 있는 행 제외" if excluded_rows > 0 else "없음",
        "used_columns": used_cols,
        "unit": "행(분반), %",
        "notes": [
            "같은 분반이 개설학과별로 여러 행에 나뉜 경우 각각 1행으로 집계됩니다.",
        ],
        "rows": rows,
        "chart": {
            "type": "bar",
            "label": f"{group_by}별 폐강률(%)",
            "labels": [str(r[group_by]) for r in rows],
            "values": [r["폐강률(%)"] for r in rows],
        },
    }


def top_bottom_n(
    df: pd.DataFrame,
    metric: str = "신청_전",
    n: int = 10,
    exclude_cancelled: bool = True,
    **kwargs,
) -> dict:
    """도구 3: 지정한 숫자 열 기준으로 상위 N개와 하위 N개 분반을 뽑습니다."""
    allowed_metrics = {"신청_전", "제한_전", "학점"}
    if metric not in allowed_metrics:
        metric = "신청_전"
    n = max(1, min(int(n), 30))

    cancelled = _cancelled_mask(df)
    valid = df[metric].notna()
    if exclude_cancelled:
        mask = (~cancelled) & valid
        reason_parts = []
        if cancelled.any():
            reason_parts.append(f"폐강 {int(cancelled.sum())}행")
        if (~valid).any():
            reason_parts.append(f"{metric} 빈칸 {int((~valid).sum())}행")
        excluded_reason = ", ".join(reason_parts) + " 제외" if reason_parts else "없음"
    else:
        mask = valid
        excluded_reason = f"{metric} 빈칸 {int((~valid).sum())}행 제외" if (~valid).any() else "없음"

    work = df[mask].copy()
    excluded_rows = int(len(df) - len(work))

    work_sorted_desc = work.sort_values(by=[metric, "교과목명"], ascending=[False, True])
    work_sorted_asc = work.sort_values(by=[metric, "교과목명"], ascending=[True, True])

    def _fmt(sub_df: pd.DataFrame, rank_type: str) -> list:
        out = []
        for _, r in sub_df.iterrows():
            out.append({
                "구분": rank_type,
                "교과목번호": str(r["교과목번호"]),
                "교과목명": str(r["교과목명"]),
                "분반": str(r["분반"]),
                "이수구분": str(r["이수구분"]),
                "개설학과": str(r["개설학과"]),
                metric: float(r[metric]) if float(r[metric]) % 1 else int(r[metric]),
            })
        return out

    top_rows = _fmt(work_sorted_desc.head(n), f"상위 {n}")
    bottom_rows = _fmt(work_sorted_asc.head(n), f"하위 {n}")
    rows = top_rows + bottom_rows

    return {
        "tool": "top_bottom_n",
        "title": f"{metric} 기준 상위·하위 {n}개 분반",
        "definition": f"{metric} 값이 큰 순서 상위 {n}개와 작은 순서 하위 {n}개 분반을 추출",
        "used_rows": int(len(work)),
        "excluded_rows": excluded_rows,
        "excluded_reason": excluded_reason,
        "used_columns": ["교과목번호", "교과목명", "분반", "이수구분", "개설학과", metric, "폐강일자"],
        "unit": "명" if "신청" in metric or "제한" in metric else "학점",
        "notes": [
            "음수(-1 등)가 포함되어 있으면 하위 목록 맨 위에 나타날 수 있습니다.",
        ],
        "rows": rows,
        "chart": {
            "type": "bar",
            "label": f"상위 {n}개 ({metric})",
            "labels": [f"{r['교과목명']}({r['분반']})" for r in top_rows],
            "values": [r[metric] for r in top_rows],
        },
    }


def fill_rate(
    df: pd.DataFrame,
    group_by: str = "단과대",
    cap_100: bool = False,
    **kwargs,
) -> dict:
    """도구 4: 폐강 제외 후 충원율(신청_전 ÷ 제한_전)을 계산합니다."""
    allowed = {"단과대", "이수구분", "대상학년"}
    if group_by not in allowed:
        group_by = "단과대"

    cancelled = _cancelled_mask(df)
    active = df[~cancelled].copy()
    bad_limit = active["제한_전"].isna() | (active["제한_전"] <= 0)
    bad_apply = active["신청_전"].isna()

    work = active[(~bad_limit) & (~bad_apply)].copy()
    excluded_cancelled = int(cancelled.sum())
    excluded_limit = int(bad_limit.sum())
    excluded_apply = int((~bad_limit & bad_apply).sum())
    excluded_rows = excluded_cancelled + excluded_limit + excluded_apply

    reason_parts = []
    if excluded_cancelled:
        reason_parts.append(f"폐강 {excluded_cancelled}행")
    if excluded_limit:
        reason_parts.append(f"제한_전 0/빈칸 {excluded_limit}행")
    if excluded_apply:
        reason_parts.append(f"신청_전 빈칸 {excluded_apply}행")
    excluded_reason = ", ".join(reason_parts) + " 제외" if reason_parts else "없음"

    work["_fill"] = work["신청_전"] / work["제한_전"] * 100.0
    over_100_count = int((work["_fill"] > 100.0).sum())
    if cap_100:
        work["_fill"] = work["_fill"].clip(upper=100.0)

    if group_by == "단과대":
        work["_group"] = _extract_college(work)
        used_cols = ["개설학과", "신청_전", "제한_전", "폐강일자"]
    else:
        work["_group"] = work[group_by].fillna("").astype(str).str.strip()
        used_cols = [group_by, "신청_전", "제한_전", "폐강일자"]

    work["_over"] = (work["신청_전"] > work["제한_전"]).astype(int)
    grouped = (
        work.groupby("_group", as_index=False)
        .agg(
            분반수=("_fill", "count"),
            평균충원율=("_fill", "mean"),
            중앙값=("_fill", "median"),
            초과분반수=("_over", "sum"),
            신청합계=("신청_전", "sum"),
            제한합계=("제한_전", "sum"),
        )
    )
    grouped["평균충원율(%)"] = grouped["평균충원율"].round(2)
    grouped["중앙값(%)"] = grouped["중앙값"].round(2)
    grouped = grouped.drop(columns=["평균충원율", "중앙값"])
    grouped["신청합계"] = grouped["신청합계"].astype(int)
    grouped["제한합계"] = grouped["제한합계"].astype(int)
    grouped = grouped.sort_values(by="평균충원율(%)", ascending=False).reset_index(drop=True)
    grouped = grouped.rename(columns={"_group": group_by})

    rows = grouped.to_dict(orient="records")
    return {
        "tool": "fill_rate",
        "title": f"{group_by}별 충원율 (신청_전 ÷ 제한_전)",
        "definition": (
            f"폐강을 제외하고 제한_전 > 0인 분반의 충원율(신청_전 ÷ 제한_전 × 100)을 {group_by}별로 평균"
            + (" (100% 상한 적용)" if cap_100 else " (100% 초과 그대로 반영)")
        ),
        "used_rows": int(len(work)),
        "excluded_rows": excluded_rows,
        "excluded_reason": excluded_reason,
        "used_columns": used_cols,
        "unit": "%",
        "notes": [
            f"100%를 초과한(신청_전 > 제한_전) 분반이 {over_100_count}건 포함되어 있습니다.",
            f"제한_전이 0이거나 비어 있어 나눌 수 없는 정상 강좌 {excluded_limit}건은 계산에서 제외했습니다.",
        ],
        "rows": rows,
        "chart": {
            "type": "bar",
            "label": f"{group_by}별 평균 충원율(%)",
            "labels": [str(r[group_by]) for r in rows],
            "values": [r["평균충원율(%)"] for r in rows],
        },
    }


def distribution(df: pd.DataFrame, exclude_cancelled: bool = True, **kwargs) -> dict:
    """도구 5: 분반당 신청 인원(신청_전) 구간별 분포를 계산합니다."""
    cancelled = _cancelled_mask(df)
    valid = df["신청_전"].notna()
    if exclude_cancelled:
        mask = (~cancelled) & valid
        reason_parts = []
        if cancelled.any():
            reason_parts.append(f"폐강 {int(cancelled.sum())}행")
        if (~valid).any():
            reason_parts.append(f"신청_전 빈칸 {int((~valid).sum())}행")
        excluded_reason = ", ".join(reason_parts) + " 제외" if reason_parts else "없음"
    else:
        mask = valid
        excluded_reason = f"신청_전 빈칸 {int((~valid).sum())}행 제외" if (~valid).any() else "없음"

    work = df[mask].copy()
    excluded_rows = int(len(df) - len(work))

    bins = [-10**9, -0.0001, 0, 10, 20, 30, 50, 100, 10**9]
    labels = ["음수(<0)", "0명", "1~10명", "11~20명", "21~30명", "31~50명", "51~100명", "101명 이상"]
    cats = pd.cut(work["신청_전"], bins=bins, labels=labels, right=True)
    vc = cats.value_counts().reindex(labels, fill_value=0)
    total = len(work) if len(work) > 0 else 1

    rows = [
        {
            "인원구간": str(lbl),
            "분반수": int(cnt),
            "비율(%)": round(int(cnt) / total * 100, 2),
        }
        for lbl, cnt in vc.items()
    ]

    return {
        "tool": "distribution",
        "title": "분반당 수강신청인원(신청_전) 구간별 분포",
        "definition": "분반별 신청_전 인원을 8개 구간으로 나누어 분반 수와 비율(%)을 집계",
        "used_rows": int(len(work)),
        "excluded_rows": excluded_rows,
        "excluded_reason": excluded_reason,
        "used_columns": ["신청_전", "폐강일자"],
        "unit": "행(분반), %",
        "notes": [
            "음수(-1 등) 값은 별도 구간('음수(<0)')으로 분리해 표시합니다.",
        ],
        "rows": rows,
        "chart": {
            "type": "bar",
            "label": "구간별 분반 수",
            "labels": [r["인원구간"] for r in rows],
            "values": [r["분반수"] for r in rows],
        },
    }


def weekday_building(df: pd.DataFrame, exclude_cancelled: bool = True, **kwargs) -> dict:
    """도구 6: 시간표를 쪼개 요일별·건물별 분반 수를 집계합니다."""
    cancelled = _cancelled_mask(df)
    has_tt = df["시간표"].notna() & df["시간표"].astype(str).str.strip().ne("")

    if exclude_cancelled:
        mask = (~cancelled) & has_tt
        excluded_cancelled = int(cancelled.sum())
        excluded_empty_tt = int((~cancelled & ~has_tt).sum())
        reason_parts = []
        if excluded_cancelled:
            reason_parts.append(f"폐강 {excluded_cancelled}행")
        if excluded_empty_tt:
            reason_parts.append(f"시간표 빈칸 {excluded_empty_tt}행")
        excluded_reason = ", ".join(reason_parts) + " 제외" if reason_parts else "없음"
    else:
        mask = has_tt
        excluded_empty_tt = int((~has_tt).sum())
        excluded_reason = f"시간표 빈칸 {excluded_empty_tt}행 제외" if excluded_empty_tt else "없음"

    work = df[mask]
    excluded_rows = int(len(df) - len(work))

    weekday_order = ["월", "화", "수", "목", "금", "토", "일"]
    weekday_counts = {d: 0 for d in weekday_order}
    building_counts = {}

    for tt in work["시간표"].astype(str):
        days_in_row = set()
        bldgs_in_row = set()
        matches = re.findall(r"([^,()]+)\(([^)]+)\)", tt)
        if matches:
            for room, inside in matches:
                b_match = re.match(r"^([가-힣A-Za-z]+)", room.strip())
                if b_match:
                    bldgs_in_row.add(b_match.group(1))
                for d in re.findall(r"[월화수목금토일]", inside):
                    days_in_row.add(d)
        else:
            for d in re.findall(r"[월화수목금토일]", tt):
                days_in_row.add(d)

        for d in days_in_row:
            weekday_counts[d] += 1
        for b in bldgs_in_row:
            building_counts[b] = building_counts.get(b, 0) + 1

    total = len(work) if len(work) > 0 else 1
    rows = []
    for d in weekday_order:
        if weekday_counts[d] > 0:
            rows.append({
                "구분": "요일",
                "항목": d,
                "분반수": weekday_counts[d],
                "비율(%)": round(weekday_counts[d] / total * 100, 2),
            })

    for b, cnt in sorted(building_counts.items(), key=lambda x: x[1], reverse=True):
        rows.append({
            "구분": "건물",
            "항목": b,
            "분반수": cnt,
            "비율(%)": round(cnt / total * 100, 2),
        })

    weekday_rows = [r for r in rows if r["구분"] == "요일"]
    return {
        "tool": "weekday_building",
        "title": "요일별·건물별 분반 수 (시간표 분석)",
        "definition": "시간표 문자열에서 요일과 건물명을 추출해 각 요일·건물에 배정된 분반 수를 집계 (한 강좌에 여러 요일·건물이 있으면 각각 1회씩 카운트)",
        "used_rows": int(len(work)),
        "excluded_rows": excluded_rows,
        "excluded_reason": excluded_reason,
        "used_columns": ["시간표", "폐강일자"],
        "unit": "행(분반), %",
        "notes": [
            "한 강좌가 여러 요일이나 여러 건물에서 열리면 각각에 1건씩 포함되므로 비율 합계가 100%를 넘을 수 있습니다.",
        ],
        "rows": rows,
        "chart": {
            "type": "bar",
            "label": "요일별 분반 수",
            "labels": [r["항목"] for r in weekday_rows],
            "values": [r["분반수"] for r in weekday_rows],
        },
    }


def compare_one_vs_all(
    df: pd.DataFrame,
    target_col: str = "단과대",
    target_value: str = None,
    metric: str = "신청_전",
    **kwargs,
) -> dict:
    """도구 7: 선택한 대상 그룹과 전체의 평균·중앙값을 비교합니다."""
    allowed_cols = {"단과대", "이수구분", "대상학년"}
    if target_col not in allowed_cols:
        target_col = "단과대"
    allowed_metrics = {"신청_전", "제한_전", "학점"}
    if metric not in allowed_metrics:
        metric = "신청_전"

    cancelled = _cancelled_mask(df)
    valid = df[metric].notna()
    mask = (~cancelled) & valid
    reason_parts = []
    if cancelled.any():
        reason_parts.append(f"폐강 {int(cancelled.sum())}행")
    if (~valid).any():
        reason_parts.append(f"{metric} 빈칸 {int((~valid).sum())}행")
    excluded_reason = ", ".join(reason_parts) + " 제외" if reason_parts else "없음"

    work = df[mask].copy()
    excluded_rows = int(len(df) - len(work))

    if target_col == "단과대":
        work["_target"] = _extract_college(work)
        used_cols = ["개설학과", metric, "폐강일자"]
    else:
        work["_target"] = work[target_col].fillna("").astype(str).str.strip()
        used_cols = [target_col, metric, "폐강일자"]

    if not target_value or target_value not in set(work["_target"]):
        target_value = str(work["_target"].value_counts().index[0])

    sub = work[work["_target"] == target_value]

    def _stats(frame: pd.DataFrame, label: str) -> dict:
        s = frame[metric]
        return {
            "대상": label,
            "분반수": int(len(s)),
            "평균": round(float(s.mean()), 2) if len(s) else 0,
            "중앙값": round(float(s.median()), 2) if len(s) else 0,
            "최솟값": round(float(s.min()), 2) if len(s) else 0,
            "최댓값": round(float(s.max()), 2) if len(s) else 0,
        }

    rows = [
        _stats(sub, f"{target_col}: {target_value}"),
        _stats(work, "전체 (폐강 제외)"),
    ]

    return {
        "tool": "compare_one_vs_all",
        "title": f"선택 대상({target_value}) vs 전체 비교 ({metric})",
        "definition": f"폐강을 제외한 전체 분반과 '{target_col}={target_value}' 분반의 {metric} 평균·중앙값·최소·최댓값을 비교",
        "used_rows": int(len(work)),
        "excluded_rows": excluded_rows,
        "excluded_reason": excluded_reason,
        "used_columns": used_cols,
        "unit": "명" if "신청" in metric or "제한" in metric else "학점",
        "notes": [
            "평균은 극단적인 큰 값(예: 대형 강좌)의 영향을 받으므로 중앙값도 함께 확인하는 것이 좋습니다.",
        ],
        "rows": rows,
        "chart": {
            "type": "bar",
            "label": f"{metric} 평균 비교",
            "labels": [r["대상"] for r in rows],
            "values": [r["평균"] for r in rows],
        },
    }


TOOL_CATALOG = {
    "quality_report": {
        "fn": quality_report,
        "description": "열별 빈칸 개수, 숫자 열의 음수/오류, 비슷한 표기 값 후보를 점검합니다.",
        "default_params": {},
    },
    "group_rate": {
        "fn": group_rate,
        "description": "단과대/이수구분/대상학년별로 전체 분반 수, 폐강 수, 폐강률을 계산합니다.",
        "default_params": {"group_by": "단과대"},
    },
    "top_bottom_n": {
        "fn": top_bottom_n,
        "description": "신청_전, 제한_전, 학점 기준으로 상위 N개와 하위 N개 분반을 찾습니다.",
        "default_params": {"metric": "신청_전", "n": 10},
    },
    "fill_rate": {
        "fn": fill_rate,
        "description": "폐강을 제외하고 제한_전 > 0인 분반의 충원율(신청_전 ÷ 제한_전)을 그룹별로 계산합니다.",
        "default_params": {"group_by": "단과대", "cap_100": False},
    },
    "distribution": {
        "fn": distribution,
        "description": "분반당 수강신청인원(신청_전) 구간별 분포를 계산합니다.",
        "default_params": {"exclude_cancelled": True},
    },
    "weekday_building": {
        "fn": weekday_building,
        "description": "시간표를 쪼개 요일별·건물별 분반 수를 집계합니다.",
        "default_params": {"exclude_cancelled": True},
    },
    "compare_one_vs_all": {
        "fn": compare_one_vs_all,
        "description": "선택한 단과대/이수구분/대상학년 하나와 전체의 평균·중앙값을 비교합니다.",
        "default_params": {"target_col": "단과대", "metric": "신청_전"},
    },
}


def run_tool(tool_name: str, df: pd.DataFrame, params: dict = None) -> dict:
    """도구 이름과 옵션(선택적 단과대/이수구분 필터 포함)으로 계산을 실행합니다."""
    if tool_name not in TOOL_CATALOG:
        raise ValueError(f"허용된 도구 목록에 없는 분석입니다: {tool_name}")

    safe_params = dict(TOOL_CATALOG[tool_name]["default_params"])
    if isinstance(params, dict):
        safe_params.update(params)

    filter_college = safe_params.pop("filter_college", None)
    filter_course_type = safe_params.pop("filter_course_type", None)

    filtered_df = df
    filter_label = ""
    filtered_out = 0
    if tool_name != "compare_one_vs_all" and (filter_college or filter_course_type):
        filtered_df, filter_label = _apply_optional_filter(
            df, college=filter_college, course_type=filter_course_type
        )
        filtered_out = int(len(df) - len(filtered_df))

    res = TOOL_CATALOG[tool_name]["fn"](filtered_df, **safe_params)
    if filter_label:
        res["title"] = f"[{filter_label}] {res['title']}"
        res["excluded_rows"] = int(res["excluded_rows"]) + filtered_out
        if res["excluded_reason"] == "없음":
            res["excluded_reason"] = f"조건 필터({filter_label}) 외 {filtered_out}행 제외"
        else:
            res["excluded_reason"] = f"조건 필터({filter_label}) 외 {filtered_out}행 + {res['excluded_reason']}"
    return res
