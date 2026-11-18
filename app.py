from difflib import SequenceMatcher
import hmac
import io
import json
import logging
import os
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from google import genai
from google.genai import types
import pandas as pd

from tools import TOOL_CATALOG, run_tool

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)


def get_configured_password() -> str:
    """환경변수 APP_PASSWORD(8자리 이상)를 확인합니다."""
    pw = os.getenv("APP_PASSWORD", "").strip()
    if not pw or pw == "change_me_1234" or len(pw) < 8:
        raise RuntimeError(".env 파일에 8자리 이상의 APP_PASSWORD를 설정해 주세요.")
    return pw


@app.before_request
def check_auth_for_api():
    """인증되지 않은 요청이 분석 API에 접근하지 못하도록 막습니다."""
    protected_paths = {"/inspect", "/suggest", "/run", "/explain", "/ask"}
    if request.path in protected_paths and not session.get("authenticated"):
        return jsonify({"error": "비밀번호(8자리 이상) 인증 후 이용할 수 있습니다."}), 401

COLUMNS = [
    "조직분류코드", "교과목번호", "교과목명", "분반", "이수구분",
    "학점", "이론", "실습", "개설학과", "대상학년",
    "신청_전", "신청_1", "신청_2", "신청_3", "신청_4",
    "제한_전", "제한_1", "제한_2", "제한_3", "제한_4",
    "시간표", "대표교수직급", "폐강일자", "폐강사유",
]

NUMERIC_COLUMNS = [
    "학점", "이론", "실습",
    "신청_전", "신청_1", "신청_2", "신청_3", "신청_4",
    "제한_전", "제한_1", "제한_2", "제한_3", "제한_4",
]

# 업로드된 데이터셋 1개만 메모리에 보관 (디스크 저장 안 함)
STATE = {
    "filename": None,
    "df": None,
    "inspect_summary": None,
    "last_result": None,
}


def load_and_parse_xls(file_bytes: bytes):
    """메모리 상의 .xls 바이트를 읽어 3행부터 데이터로 쓰고 숫자 열을 변환합니다."""
    raw_df = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="xlrd")
    if len(raw_df) < 3 or raw_df.shape[1] != len(COLUMNS):
        raise ValueError(
            f"예상한 표 구조(3행 이상, {len(COLUMNS)}열)와 다릅니다. "
            f"(현재: {len(raw_df)}행, {raw_df.shape[1]}열)"
        )

    df = raw_df.iloc[2:].copy().reset_index(drop=True)
    df.columns = COLUMNS

    numeric_issues = []
    for col in NUMERIC_COLUMNS:
        orig = df[col]
        converted = pd.to_numeric(orig, errors="coerce")

        # 원본에는 값이 있었는데 숫자로 바꾸지 못한 경우
        failed_mask = orig.notna() & orig.astype(str).str.strip().ne("") & converted.isna()
        if failed_mask.any():
            rows = [int(i) + 3 for i in df.index[failed_mask]]
            numeric_issues.append({
                "column": col,
                "issue_type": "숫자 변환 실패",
                "count": len(rows),
                "excel_rows": rows[:20],
            })

        # 음수인 경우 (몰래 고치지 않고 그대로 두고 위치만 알림)
        neg_mask = converted.notna() & (converted < 0)
        if neg_mask.any():
            rows = [int(i) + 3 for i in df.index[neg_mask]]
            values = [float(v) for v in converted[neg_mask]]
            numeric_issues.append({
                "column": col,
                "issue_type": "음수 값",
                "count": len(rows),
                "excel_rows": rows[:20],
                "sample_values": values[:5],
            })

        df[col] = converted

    return df, numeric_issues


def find_missing_by_column(df: pd.DataFrame) -> dict:
    """각 열의 빈칸(결측·공백 문자열) 개수를 셉니다."""
    missing = {}
    for col in COLUMNS:
        s = df[col]
        if col in NUMERIC_COLUMNS:
            cnt = int(s.isna().sum())
        else:
            cnt = int((s.isna() | s.astype(str).str.strip().eq("")).sum())
        if cnt > 0:
            missing[col] = cnt
    return missing


def find_similar_value_candidates(df: pd.DataFrame) -> list:
    """주요 분류 열에서 같은 뜻인데 표기만 다른 것처럼 보이는 값 후보를 찾습니다."""
    candidates = []
    for col in ["이수구분", "대표교수직급", "폐강사유"]:
        vc = df[col].dropna().astype(str).str.strip()
        vc = vc[vc.ne("")].value_counts()
        values = list(vc.index)

        seen_pairs = set()
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                a, b = values[i], values[j]
                a_ns, b_ns = a.replace(" ", ""), b.replace(" ", "")
                ratio = SequenceMatcher(None, a_ns, b_ns).ratio()
                is_space_diff = (a_ns == b_ns)
                is_similar_text = (
                    (len(a_ns) >= 4 and len(b_ns) >= 4)
                    and (a_ns in b_ns or b_ns in a_ns or ratio >= 0.72)
                )
                if is_space_diff or is_similar_text:
                    pair_key = tuple(sorted([a, b]))
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        candidates.append({
                            "column": col,
                            "values": [
                                {"value": a, "count": int(vc[a])},
                                {"value": b, "count": int(vc[b])},
                            ],
                            "reason": "띄어쓰기 차이" if is_space_diff else "비슷한 문구",
                        })
    return candidates


def build_inspect_summary(filename: str, df: pd.DataFrame, numeric_issues: list) -> dict:
    """업로드된 파일의 진단 요약과 경고 목록을 생성합니다."""
    missing_by_column = find_missing_by_column(df)
    similar_candidates = find_similar_value_candidates(df)

    # 단과대 분리 (개설학과를 '>>'로 나눈 첫 부분)
    colleges_series = (
        df["개설학과"]
        .dropna()
        .astype(str)
        .str.split(">>")
        .str[0]
        .str.strip()
    )
    colleges = sorted([c for c in colleges_series.unique() if c])

    # 폐강 여부 및 분반/과목 수
    cancelled_mask = df["폐강일자"].notna() & df["폐강일자"].astype(str).str.strip().ne("")
    cancelled_count = int(cancelled_mask.sum())
    active_count = int(len(df) - cancelled_count)

    active_df = df[~cancelled_mask]
    limit_zero_active = int((active_df["제한_전"].isna() | (active_df["제한_전"] == 0)).sum())
    over_limit_active = int(((active_df["제한_전"] > 0) & (active_df["신청_전"] > active_df["제한_전"])).sum())

    unique_course_section_count = int(df.drop_duplicates(subset=["교과목번호", "분반"]).shape[0])
    unique_course_code_count = int(df["교과목번호"].dropna().astype(str).nunique())
    unique_course_name_count = int(df["교과목명"].dropna().astype(str).nunique())

    warnings = [
        {
            "type": "열 이름 2줄 처리",
            "message": "엑셀 1~2행의 병합 제목을 건너뛰고 3행부터 데이터로 읽었으며, 24개 열 이름을 직접 지정했습니다.",
        },
        {
            "type": "글자 숫자 변환",
            "message": "학점·이론·실습·신청·제한 인원 등 13개 숫자 열이 글자로 저장되어 있어 숫자로 변환했습니다.",
        },
    ]

    if numeric_issues or limit_zero_active > 0:
        issue_parts = []
        for item in numeric_issues:
            issue_parts.append(
                f"{item['column']} {item['issue_type']} {item['count']}건(엑셀 {item['excel_rows']}행)"
            )
        if limit_zero_active > 0:
            issue_parts.append(f"폐강 제외 강좌 중 제한_전이 0/빈칸 {limit_zero_active}건")
        warnings.append({
            "type": "비정상 값",
            "message": "확인 필요한 숫자 값이 있습니다: " + ", ".join(issue_parts),
        })

    if missing_by_column:
        missing_text = ", ".join(f"{col}({cnt}개)" for col, cnt in missing_by_column.items())
        warnings.append({
            "type": "빈칸",
            "message": (
                f"빈칸이 있는 열: {missing_text}. "
                "(폐강일자·폐강사유 빈칸은 정상 개설 '해당 없음'이며, 시간표·대표교수직급 빈칸은 누락 여부를 파일만으로 알 수 없습니다.)"
            ),
        })

    if similar_candidates:
        cand_text = "; ".join(
            f"{c['column']}: '{c['values'][0]['value']}'({c['values'][0]['count']}) vs '{c['values'][1]['value']}'({c['values'][1]['count']})"
            for c in similar_candidates
        )
        warnings.append({
            "type": "비슷한 값 후보",
            "message": f"같은 뜻인데 다르게 적힌 것처럼 보이는 값이 있습니다: {cand_text}",
        })

    if unique_course_section_count < len(df):
        warnings.append({
            "type": "행 단위 참고",
            "message": (
                f"전체 {len(df)}행 중 (교과목번호, 분반) 기준 고유 분반은 {unique_course_section_count}개입니다. "
                "같은 분반이 개설학과별로 여러 행에 나뉜 경우가 있습니다."
            ),
        })

    return {
        "filename": filename,
        "row_count": int(len(df)),
        "col_count": int(df.shape[1]),
        "columns": COLUMNS,
        "numeric_columns": NUMERIC_COLUMNS,
        "numeric_issues": numeric_issues,
        "missing_by_column": missing_by_column,
        "similar_value_candidates": similar_candidates,
        "college_count": len(colleges),
        "colleges": colleges,
        "counts": {
            "total_rows": int(len(df)),
            "active_rows": active_count,
            "cancelled_rows": cancelled_count,
            "unique_course_sections": unique_course_section_count,
            "unique_course_codes": unique_course_code_count,
            "unique_course_names": unique_course_name_count,
            "limit_zero_active_rows": limit_zero_active,
            "over_limit_active_rows": over_limit_active,
        },
        "warnings": warnings,
    }


@app.get("/")
def index():
    """메인 웹 화면(index.html)을 열어 줍니다."""
    return render_template("index.html")


@app.get("/health")
def health():
    """서버가 정상 작동 중인지 확인하는 상태 점검 창구입니다."""
    logger.info("Health check requested")
    return jsonify({"status": "ok"}), 200


@app.get("/auth/status")
def auth_status():
    """현재 브라우저의 비밀번호 인증 여부를 반환합니다."""
    return jsonify({"authenticated": bool(session.get("authenticated"))}), 200


@app.post("/auth/login")
def auth_login():
    """8자리 이상 비밀번호를 확인해 앱 잠금을 해제합니다."""
    payload = request.get_json(silent=True) or {}
    entered = str(payload.get("password", "")).strip()
    if len(entered) < 8:
        return jsonify({"error": "비밀번호는 8자리 이상 입력해야 합니다."}), 400

    try:
        configured = get_configured_password()
    except RuntimeError as exc:
        logger.warning("APP_PASSWORD configuration error: %s", exc)
        return jsonify({"error": str(exc)}), 500

    if not hmac.compare_digest(entered, configured):
        logger.warning("Failed login attempt")
        return jsonify({"error": "비밀번호가 올바르지 않습니다."}), 401

    session["authenticated"] = True
    logger.info("User authenticated")
    return jsonify({"authenticated": True}), 200


@app.post("/auth/logout")
def auth_logout():
    """앱을 다시 잠급니다."""
    session.pop("authenticated", None)
    return jsonify({"authenticated": False}), 200


@app.post("/inspect")
def inspect_file():
    """업로드된 .xls 파일을 메모리에서 읽어 진단 요약과 경고 목록을 반환합니다."""
    if "file" not in request.files:
        return jsonify({"error": "업로드할 파일이 선택되지 않았습니다."}), 400

    uploaded = request.files["file"]
    filename = uploaded.filename or ""
    if not filename:
        return jsonify({"error": "파일 이름이 비어 있습니다. .xls 파일을 선택해 주세요."}), 400
    if not filename.lower().endswith(".xls"):
        return jsonify({"error": "확장자가 .xls인 엑셀 파일만 올릴 수 있습니다."}), 400

    file_bytes = uploaded.read()
    if not file_bytes:
        return jsonify({"error": "비어 있는 파일입니다."}), 400

    try:
        df, numeric_issues = load_and_parse_xls(file_bytes)
    except ValueError as exc:
        logger.warning("Invalid Excel structure for %s: %s", filename, exc)
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Failed to read Excel file: %s", filename)
        return jsonify({"error": "엑셀(.xls) 파일을 읽는 중 오류가 발생했습니다. 파일 형식을 확인해 주세요."}), 400

    summary = build_inspect_summary(filename, df, numeric_issues)

    STATE["filename"] = filename
    STATE["df"] = df
    STATE["inspect_summary"] = summary
    STATE["last_result"] = None

    logger.info(
        "Inspected file %s: %d rows, %d warnings",
        filename,
        len(df),
        len(summary["warnings"]),
    )
    return jsonify(summary), 200


GEMINI_MODEL = "gemini-3.5-flash-lite"


def get_gemini_client():
    """환경변수 GEMINI_API_KEY를 사용해 Gemini 클라이언트를 만듭니다."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_gemini_api_key_here":
        raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다. .env 파일에 실제 API 키를 입력해 주세요.")
    return genai.Client(api_key=api_key)


def filter_valid_suggestions(raw_items: list) -> tuple[list, list]:
    """도구 목록(TOOL_CATALOG)에 있는 제안만 통과시키고, 목록 밖의 제안은 거절합니다."""
    accepted = []
    rejected = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool", "")).strip()
        if tool_name not in TOOL_CATALOG:
            rejected.append(tool_name or "(이름 없음)")
            continue
        accepted.append({
            "tool": tool_name,
            "reason": str(item.get("reason", "")).strip() or TOOL_CATALOG[tool_name]["description"],
            "columns": item.get("columns", []),
            "params": item.get("params") if isinstance(item.get("params"), dict) else TOOL_CATALOG[tool_name]["default_params"],
        })
    return accepted[:5], rejected


@app.post("/suggest")
def suggest_analyses():
    """진단 요약과 도구 목록만 AI에게 보내 실행 가능한 분석 3~5개를 JSON으로 제안받습니다."""
    summary = STATE.get("inspect_summary")
    if not summary:
        return jsonify({"error": "먼저 엑셀(.xls) 파일을 업로드하고 진단을 실행해 주세요."}), 400

    catalog_info = [
        {
            "tool": name,
            "description": info["description"],
            "default_params": info["default_params"],
        }
        for name, info in TOOL_CATALOG.items()
    ]

    prompt = (
        "당신은 대학 학사 행정 데이터 분석 도우미입니다.\n"
        "아래 '진단 요약'을 보고, '허용된 도구 목록' 중에서 이 데이터에 유용한 분석 3~5개를 골라 JSON으로 제안하세요.\n"
        "규칙:\n"
        "1. 반드시 허용된 도구 목록에 있는 tool 이름만 사용하세요. 목록에 없는 분석이나 임의의 코드는 절대 제안하지 마세요.\n"
        "2. 출력은 반드시 아래 형식의 JSON 객체여야 합니다:\n"
        '{"suggestions": [{"tool": "도구이름", "reason": "이 분석을 추천하는 쉬운 이유", "columns": ["사용할 열"], "params": {}}]}\n'
        "3. reason은 코딩을 모르는 대학 행정 직원이 이해할 수 있는 쉬운 한국어로 작성하세요.\n\n"
        f"[허용된 도구 목록]\n{json.dumps(catalog_info, ensure_ascii=False)}\n\n"
        f"[진단 요약 (원본 행 없음)]\n{json.dumps(summary, ensure_ascii=False)}"
    )

    try:
        client = get_gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        parsed = json.loads(response.text)
    except RuntimeError as exc:
        logger.warning("Gemini config error: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Failed to get suggestions from Gemini")
        return jsonify({"error": "AI에게 분석 제안을 받는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."}), 502

    raw_list = parsed.get("suggestions", []) if isinstance(parsed, dict) else []
    accepted, rejected = filter_valid_suggestions(raw_list)
    if rejected:
        logger.warning("Rejected out-of-catalog AI suggestions: %s", rejected)

    if not accepted:
        return jsonify({"error": "AI가 허용된 도구 목록 안의 분석을 제안하지 못했습니다. 다시 시도해 주세요."}), 502

    logger.info("Suggested %d valid analyses (rejected %d)", len(accepted), len(rejected))
    return jsonify({"suggestions": accepted, "rejected_tools": rejected}), 200


@app.post("/run")
def run_analyses():
    """사용자가 선택한 도구들을 pandas로 계산하고 결과를 메모리에 보관합니다."""
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "먼저 엑셀(.xls) 파일을 업로드하고 진단을 실행해 주세요."}), 400

    payload = request.get_json(silent=True) or {}
    analyses = payload.get("analyses")
    if analyses is None and payload.get("tool"):
        analyses = [{"tool": payload.get("tool"), "params": payload.get("params") or {}}]

    if not isinstance(analyses, list) or len(analyses) == 0:
        return jsonify({"error": "실행할 분석을 1개 이상 선택해 주세요."}), 400

    results = []
    for item in analyses:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool", "")).strip()
        if tool_name not in TOOL_CATALOG:
            logger.warning("Blocked unknown tool in /run: %s", tool_name)
            return jsonify({"error": f"허용된 도구 목록에 없는 분석입니다: {tool_name}"}), 400

        params = item.get("params")
        if not isinstance(params, dict):
            params = dict(TOOL_CATALOG[tool_name]["default_params"])

        try:
            res = run_tool(tool_name, df, params)
            results.append(res)
        except Exception:
            logger.exception("Failed running tool %s", tool_name)
            return jsonify({"error": f"'{tool_name}' 계산 중 오류가 발생했습니다."}), 500

    if not results:
        return jsonify({"error": "실행할 유효한 분석 항목이 없습니다."}), 400

    STATE["last_result"] = {
        "filename": STATE["filename"],
        "results": results,
    }
    logger.info("Ran %d analyses for %s", len(results), STATE["filename"])
    return jsonify(STATE["last_result"]), 200


@app.post("/explain")
def explain_report():
    """모드 A(진단 요약만) 또는 모드 B(계산 결과 JSON만)로 AI 해석 보고서를 생성합니다."""
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode", "B")).strip().upper()
    if mode not in ("A", "B"):
        return jsonify({"error": "보고서 모드는 'A' 또는 'B'만 선택할 수 있습니다."}), 400

    summary = STATE.get("inspect_summary")
    if not summary:
        return jsonify({"error": "먼저 엑셀(.xls) 파일을 업로드하고 진단을 실행해 주세요."}), 400

    if mode == "A":
        # 비교용 모드 A: 일부러 진단 요약만 주고 요청
        prompt = (
            "아래 대학 개설강의 엑셀 파일의 진단 요약만 보고 분석해서 설명해 줘.\n\n"
            f"[진단 요약]\n{json.dumps(summary, ensure_ascii=False)}"
        )
    else:
        # 정식 모드 B: 계산 결과 JSON만 주고 엄격한 지시 + 7단 구성 요청
        last_result = STATE.get("last_result")
        if not last_result:
            return jsonify({"error": "모드 B 보고서를 만들려면 먼저 분석을 선택하고 계산을 실행해 주세요."}), 400

        prompt = (
            "당신은 대학 학사 행정 데이터 보고서 작성 도우미입니다.\n"
            "아래 '계산 결과 JSON'만을 근거로 코딩을 모르는 행정 직원이 읽기 쉬운 마크다운(.md) 보고서를 작성하세요.\n\n"
            "필수 규칙:\n"
            "- 주어진 숫자만 인용하라. 새 숫자를 만들거나 다시 계산하지 마라. 파일만으로 알 수 없는 것은 알 수 없다고 써라.\n"
            "- 원본 행이나 데이터에 없는 내용을 추측해서 채우지 마라.\n\n"
            "보고서 구성 (반드시 아래 7개 제목을 순서대로 사용):\n"
            "## 1. 무엇을 봤는가\n"
            "## 2. 기준(정의)과 사용한 열\n"
            "## 3. 제외된 항목\n"
            "## 4. 계산 결과\n"
            "## 5. 읽어낼 수 있는 것\n"
            "## 6. 이 데이터로는 알 수 없는 것\n"
            "## 7. 다음에 확인해볼 것\n\n"
            f"[계산 결과 JSON]\n{json.dumps(last_result, ensure_ascii=False)}"
        )

    try:
        client = get_gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        report_text = response.text or ""
    except RuntimeError as exc:
        logger.warning("Gemini config error in /explain: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Failed to generate report in /explain (mode=%s)", mode)
        return jsonify({"error": "AI 보고서를 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."}), 502

    logger.info("Generated report in mode %s (%d chars)", mode, len(report_text))
    return jsonify({"mode": mode, "report": report_text}), 200


@app.post("/ask")
def ask_question():
    """사람의 자연어 질문을 받아 알맞은 도구로 pandas 계산을 수행하고 답변을 반환합니다."""
    df = STATE.get("df")
    summary = STATE.get("inspect_summary")
    if df is None or not summary:
        return jsonify({"error": "먼저 엑셀(.xls) 파일을 업로드하고 진단을 실행해 주세요."}), 400

    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not question:
        return jsonify({"error": "궁금한 질문을 입력해 주세요."}), 400

    catalog_info = [
        {
            "tool": name,
            "description": info["description"],
            "default_params": info["default_params"],
            "optional_filters": ["filter_college (단과대명)", "filter_course_type (이수구분)"],
        }
        for name, info in TOOL_CATALOG.items()
    ]

    select_prompt = (
        "당신은 대학 학사 행정 데이터 분석 안내원입니다.\n"
        "사용자의 질문을 보고, 아래 '허용된 도구 목록' 중 하나로 답할 수 있는지 판단하여 JSON으로 응답하세요.\n"
        "규칙:\n"
        "1. 반드시 허용된 도구 목록 안의 tool 이름만 고르세요. 임의의 코드를 만들지 마세요.\n"
        "2. 파일에 없는 정보(예: 교수 이름, 학생 개인정보, 성별 등)나 7개 도구로 계산할 수 없는 질문이면 answerable을 false로 하세요.\n"
        "3. 특정 단과대나 이수구분이 질문에 언급되면 params에 filter_college 또는 filter_course_type을 넣을 수 있습니다.\n"
        "4. compare_one_vs_all 사용 시 target_col('단과대'|'이수구분'|'대상학년')과 target_value를 지정하세요.\n"
        "5. 출력 JSON 형식:\n"
        '{"answerable": true, "tool": "도구이름", "params": {}, "reason": "선택 이유", "cannot_answer_reason": ""}\n\n'
        f"[허용된 도구 목록]\n{json.dumps(catalog_info, ensure_ascii=False)}\n\n"
        f"[진단 요약 (원본 행 없음)]\n{json.dumps(summary, ensure_ascii=False)}\n\n"
        f"[사용자 질문]\n{question}"
    )

    try:
        client = get_gemini_client()
        sel_resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=select_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        selection = json.loads(sel_resp.text)
    except RuntimeError as exc:
        logger.warning("Gemini config error in /ask: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Failed to interpret question in /ask")
        return jsonify({"error": "질문을 해석하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."}), 502

    if not isinstance(selection, dict) or not selection.get("answerable"):
        msg = (
            selection.get("cannot_answer_reason")
            if isinstance(selection, dict)
            else None
        ) or "현재 파일의 열과 허용된 7개 분석 도구로는 답할 수 없는 질문입니다."
        logger.info("Question marked unanswerable: %s", msg)
        return jsonify({"answerable": False, "message": msg}), 200

    tool_name = str(selection.get("tool", "")).strip()
    if tool_name not in TOOL_CATALOG:
        logger.warning("Blocked out-of-catalog tool in /ask: %s", tool_name)
        return jsonify({
            "answerable": False,
            "message": "허용된 7개 분석 도구 범위 밖의 요청이라 실행할 수 없습니다.",
        }), 200

    params = selection.get("params") if isinstance(selection.get("params"), dict) else {}
    try:
        calc_res = run_tool(tool_name, df, params)
    except Exception:
        logger.exception("Failed running tool %s in /ask", tool_name)
        return jsonify({"error": "질문에 대한 계산을 실행하는 중 오류가 발생했습니다."}), 500

    STATE["last_result"] = {
        "filename": STATE["filename"],
        "results": [calc_res],
    }

    answer_prompt = (
        "당신은 대학 학사 행정 데이터 보고서 작성 도우미입니다.\n"
        "아래 '사용자 질문'에 대해 '계산 결과 JSON'만을 근거로 마크다운(.md) 답변을 작성하세요.\n\n"
        "필수 규칙:\n"
        "- 주어진 숫자만 인용하라. 새 숫자를 만들거나 다시 계산하지 마라. 파일만으로 알 수 없는 것은 알 수 없다고 써라.\n"
        "- 사용한 행 수와 제외한 행 수·이유를 반드시 밝히세요.\n\n"
        f"[사용자 질문]\n{question}\n\n"
        f"[계산 결과 JSON]\n{json.dumps(calc_res, ensure_ascii=False)}"
    )

    try:
        ans_resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=answer_prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        answer_text = ans_resp.text or ""
    except Exception:
        logger.exception("Failed to generate answer text in /ask")
        answer_text = "계산은 완료되었으나 해석 문장을 생성하지 못했습니다. 위 표와 차트를 확인해 주세요."

    logger.info("Answered question with tool %s (%d used rows)", tool_name, calc_res["used_rows"])
    return jsonify({
        "answerable": True,
        "question": question,
        "selection": {"tool": tool_name, "params": params, "reason": selection.get("reason", "")},
        "results": [calc_res],
        "report": answer_text,
    }), 200


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
