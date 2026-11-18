const passwordInput = document.getElementById("password-input");
const btnLogin = document.getElementById("btn-login");
const btnLogout = document.getElementById("btn-logout");
const authLocked = document.getElementById("auth-locked");
const authUnlocked = document.getElementById("auth-unlocked");
const workspaceSections = document.getElementById("workspace-sections");

const fileInput = document.getElementById("file-input");
const btnInspect = document.getElementById("btn-inspect");
const btnSuggest = document.getElementById("btn-suggest");
const btnRun = document.getElementById("btn-run");
const questionInput = document.getElementById("question-input");
const btnAsk = document.getElementById("btn-ask");
const btnExplain = document.getElementById("btn-explain");
const btnPreview = document.getElementById("btn-preview");
const btnCopy = document.getElementById("btn-copy");
const btnDownload = document.getElementById("btn-download");

const statusBanner = document.getElementById("status-banner");
const inspectEmpty = document.getElementById("inspect-empty");
const inspectContent = document.getElementById("inspect-content");
const inspectSummaryGrid = document.getElementById("inspect-summary-grid");
const warningsList = document.getElementById("warnings-list");
const missingTableWrap = document.getElementById("missing-table-wrap");

const suggestEmpty = document.getElementById("suggest-empty");
const suggestList = document.getElementById("suggest-list");
const resultEmpty = document.getElementById("result-empty");
const resultContainer = document.getElementById("result-container");
const reportOutput = document.getElementById("report-output");
const reportPreview = document.getElementById("report-preview");

let currentSuggestions = [];
let activeCharts = [];
let isPreviewMode = false;

function showBanner(message, type = "info") {
  statusBanner.textContent = message;
  statusBanner.className = `banner ${type}`;
}

function setBusy(isBusy, message = "처리 중...") {
  const buttons = [btnInspect, btnSuggest, btnRun, btnAsk, btnExplain];
  buttons.forEach((b) => {
    b.disabled = isBusy;
  });
  if (isBusy) {
    showBanner(message, "info");
  }
}

function setAuthUI(authenticated) {
  if (authenticated) {
    authLocked.classList.add("hidden");
    authUnlocked.classList.remove("hidden");
    workspaceSections.classList.remove("hidden");
  } else {
    authLocked.classList.remove("hidden");
    authUnlocked.classList.add("hidden");
    workspaceSections.classList.add("hidden");
  }
}

function syncReportButtons() {
  const hasReport = reportOutput.value.trim().length > 0;
  btnPreview.disabled = !hasReport;
  btnCopy.disabled = !hasReport;
  btnDownload.disabled = !hasReport;
}

function setPreviewMode(previewOn) {
  isPreviewMode = previewOn;
  if (isPreviewMode) {
    const mdText = reportOutput.value || "";
    if (typeof marked !== "undefined") {
      reportPreview.innerHTML = marked.parse(mdText);
    } else {
      reportPreview.textContent = mdText;
    }
    reportOutput.classList.add("hidden");
    reportPreview.classList.remove("hidden");
    btnPreview.textContent = "편집하기";
  } else {
    reportPreview.classList.add("hidden");
    reportOutput.classList.remove("hidden");
    btnPreview.textContent = "미리보기";
  }
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderInspectResult(data) {
  inspectEmpty.classList.add("hidden");
  inspectContent.classList.remove("hidden");

  const c = data.counts || {};
  const stats = [
    { label: "파일명", value: data.filename },
    { label: "전체 데이터 행", value: `${c.total_rows ?? data.row_count}행` },
    { label: "정상 개설 / 폐강", value: `${c.active_rows ?? 0} / ${c.cancelled_rows ?? 0}` },
    { label: "고유 분반 / 과목번호", value: `${c.unique_course_sections ?? 0} / ${c.unique_course_codes ?? 0}` },
    { label: "단과대 수", value: `${data.college_count ?? 0}개` },
  ];

  inspectSummaryGrid.innerHTML = stats
    .map(
      (s) => `
      <div class="stat-box">
        <div class="stat-label">${escapeHtml(s.label)}</div>
        <div class="stat-value">${escapeHtml(s.value)}</div>
      </div>`
    )
    .join("");

  warningsList.innerHTML = (data.warnings || [])
    .map((w) => `<li><strong>[${escapeHtml(w.type)}]</strong> ${escapeHtml(w.message)}</li>`)
    .join("");

  const missingEntries = Object.entries(data.missing_by_column || {});
  if (missingEntries.length === 0) {
    missingTableWrap.innerHTML = `<p class="help-text">빈칸이 있는 열이 없습니다.</p>`;
  } else {
    missingTableWrap.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr><th>열 이름</th><th>빈칸 개수</th></tr></thead>
          <tbody>
            ${missingEntries
              .map(([col, cnt]) => `<tr><td>${escapeHtml(col)}</td><td>${escapeHtml(cnt)}개</td></tr>`)
              .join("")}
          </tbody>
        </table>
      </div>`;
  }
}

function renderSuggestions(suggestions) {
  currentSuggestions = suggestions || [];
  if (currentSuggestions.length === 0) {
    suggestEmpty.classList.remove("hidden");
    suggestList.innerHTML = "";
    btnRun.disabled = true;
    return;
  }

  suggestEmpty.classList.add("hidden");
  suggestList.innerHTML = currentSuggestions
    .map(
      (item, idx) => `
      <label class="suggest-item">
        <input type="checkbox" class="suggest-checkbox" value="${idx}" checked />
        <div>
          <div class="suggest-title">${escapeHtml(item.tool)}</div>
          <div>${escapeHtml(item.reason)}</div>
          <div class="suggest-meta">사용 열: ${escapeHtml((item.columns || []).join(", ") || "자동 선택")}</div>
        </div>
      </label>`
    )
    .join("");

  btnRun.disabled = false;
}

function destroyCharts() {
  activeCharts.forEach((ch) => ch.destroy());
  activeCharts = [];
}

function renderResults(results) {
  destroyCharts();
  if (!results || results.length === 0) {
    resultEmpty.classList.remove("hidden");
    resultContainer.innerHTML = "";
    return;
  }

  resultEmpty.classList.add("hidden");
  resultContainer.innerHTML = "";

  results.forEach((res, idx) => {
    const card = document.createElement("div");
    card.className = "result-card";

    const rows = res.rows || [];
    const headers = rows.length > 0 ? Object.keys(rows[0]) : [];

    const tableHtml =
      rows.length === 0
        ? `<p class="help-text">표시할 결과 행이 없습니다.</p>`
        : `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr>
            </thead>
            <tbody>
              ${rows
                .map(
                  (r) =>
                    `<tr>${headers.map((h) => `<td>${escapeHtml(r[h])}</td>`).join("")}</tr>`
                )
                .join("")}
            </tbody>
          </table>
        </div>`;

    const chartId = `result-chart-${idx}`;
    const hasChart = res.chart && Array.isArray(res.chart.labels) && res.chart.labels.length > 0;

    card.innerHTML = `
      <h3>${escapeHtml(res.title || res.tool)}</h3>
      <div class="meta-box">
        <p><strong>기준(정의):</strong> ${escapeHtml(res.definition)}</p>
        <p><strong>사용한 행 수:</strong> ${escapeHtml(res.used_rows)}행 | <strong>제외한 행 수:</strong> ${escapeHtml(res.excluded_rows)}행 (${escapeHtml(res.excluded_reason)})</p>
        <p><strong>사용한 열:</strong> ${escapeHtml((res.used_columns || []).join(", "))} | <strong>단위:</strong> ${escapeHtml(res.unit)}</p>
        <p><strong>주의사항:</strong> ${escapeHtml((res.notes || []).join(" / ") || "없음")}</p>
      </div>
      ${tableHtml}
      ${hasChart ? `<div class="chart-wrap"><canvas id="${chartId}"></canvas></div>` : ""}
    `;

    resultContainer.appendChild(card);

    if (hasChart && typeof Chart !== "undefined") {
      const ctx = document.getElementById(chartId).getContext("2d");
      const chart = new Chart(ctx, {
        type: res.chart.type || "bar",
        data: {
          labels: res.chart.labels.slice(0, 20),
          datasets: [
            {
              label: res.chart.label || res.title,
              data: res.chart.values.slice(0, 20),
              backgroundColor: "rgba(37, 99, 235, 0.7)",
              borderColor: "rgba(30, 64, 175, 1)",
              borderWidth: 1,
            },
          ],
        },
        options: {
          responsive: true,
          plugins: { legend: { display: true } },
          scales: { y: { beginAtZero: true } },
        },
      });
      activeCharts.push(chart);
    }
  });
}

async function checkAuthStatus() {
  try {
    const resp = await fetch("/auth/status");
    const data = await resp.json();
    setAuthUI(Boolean(data.authenticated));
    console.log("[인증 상태]", data.authenticated);
  } catch (err) {
    console.error("인증 상태 확인 오류:", err);
  }
}

btnLogin.addEventListener("click", async () => {
  const pw = passwordInput.value.trim();
  if (pw.length < 8) {
    showBanner("비밀번호는 8자리 이상 입력해야 합니다.", "error");
    return;
  }
  console.log("[인증] 잠금 해제 요청");
  try {
    const resp = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "비밀번호 확인에 실패했습니다.");
    }
    passwordInput.value = "";
    setAuthUI(true);
    showBanner("앱 잠금이 해제되었습니다.", "success");
  } catch (err) {
    console.error("[인증] 오류:", err);
    showBanner(err.message, "error");
  }
});

btnLogout.addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  setAuthUI(false);
  showBanner("앱이 다시 잠겼습니다.", "info");
});

btnInspect.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) {
    showBanner("먼저 엑셀(.xls) 파일을 선택해 주세요.", "error");
    return;
  }
  if (!file.name.toLowerCase().endsWith(".xls")) {
    showBanner("확장자가 .xls인 엑셀 파일만 업로드할 수 있습니다.", "error");
    return;
  }

  console.log("[1단계] 파일 업로드 및 진단 시작:", file.name);
  setBusy(true, "처리 중... 엑셀 파일을 읽고 진단하고 있습니다.");
  try {
    const formData = new FormData();
    formData.append("file", file);
    const resp = await fetch("/inspect", { method: "POST", body: formData });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "파일 진단 중 오류가 발생했습니다.");
    }
    renderInspectResult(data);
    renderSuggestions([]);
    renderResults([]);
    reportOutput.value = "";
    setPreviewMode(false);
    syncReportButtons();
    console.log("[1단계] 파일 진단 완료:", data);
    showBanner(`진단 완료: ${data.filename} (${data.row_count}행)`, "success");
  } catch (err) {
    console.error("[1단계] 오류:", err);
    showBanner(err.message, "error");
  } finally {
    setBusy(false);
    btnRun.disabled = currentSuggestions.length === 0;
  }
});

btnSuggest.addEventListener("click", async () => {
  console.log("[2단계] AI 분석 제안 요청 시작");
  setBusy(true, "처리 중... AI가 진단 요약을 보고 분석을 제안하고 있습니다.");
  try {
    const resp = await fetch("/suggest", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "AI 제안을 불러오지 못했습니다.");
    }
    renderSuggestions(data.suggestions || []);
    console.log("[2단계] AI 제안 수신 완료:", data.suggestions);
    showBanner(`AI가 ${(data.suggestions || []).length}개의 분석을 제안했습니다. 원하는 항목을 체크한 뒤 계산을 실행하세요.`, "success");
  } catch (err) {
    console.error("[2단계] 오류:", err);
    showBanner(err.message, "error");
  } finally {
    setBusy(false);
    btnRun.disabled = currentSuggestions.length === 0;
  }
});

btnRun.addEventListener("click", async () => {
  const checkedBoxes = Array.from(document.querySelectorAll(".suggest-checkbox:checked"));
  if (checkedBoxes.length === 0) {
    showBanner("실행할 분석을 1개 이상 체크해 주세요.", "error");
    return;
  }

  const selected = checkedBoxes.map((cb) => {
    const item = currentSuggestions[Number(cb.value)];
    return { tool: item.tool, params: item.params || {} };
  });

  console.log("[3단계] pandas 계산 실행 시작:", selected);
  setBusy(true, "처리 중... pandas가 선택한 분석을 계산하고 있습니다.");
  try {
    const resp = await fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ analyses: selected }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "계산 실행 중 오류가 발생했습니다.");
    }
    renderResults(data.results || []);
    console.log("[3단계] 계산 완료:", data.results);
    showBanner(`계산 완료: ${(data.results || []).length}개 분석 결과를 확인하세요.`, "success");
  } catch (err) {
    console.error("[3단계] 오류:", err);
    showBanner(err.message, "error");
  } finally {
    setBusy(false);
    btnRun.disabled = currentSuggestions.length === 0;
  }
});

btnAsk.addEventListener("click", async () => {
  const question = questionInput.value.trim();
  if (!question) {
    showBanner("궁금한 질문을 먼저 입력해 주세요.", "error");
    return;
  }

  console.log("[질문 분석] 시작:", question);
  setBusy(true, "처리 중... 질문을 분석 도구로 연결하고 pandas로 계산하고 있습니다.");
  try {
    const resp = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "질문 분석 중 오류가 발생했습니다.");
    }
    if (!data.answerable) {
      showBanner(data.message || "현재 데이터·도구로는 답할 수 없는 질문입니다.", "error");
      return;
    }

    renderResults(data.results || []);
    reportOutput.value = data.report || "";
    setPreviewMode(false);
    syncReportButtons();
    console.log("[질문 분석] 완료:", data.selection);
    showBanner(`질문 분석 완료 (사용 도구: ${data.selection.tool}). 계산 결과와 보고서를 확인하세요.`, "success");
  } catch (err) {
    console.error("[질문 분석] 오류:", err);
    showBanner(err.message, "error");
  } finally {
    setBusy(false);
    btnRun.disabled = currentSuggestions.length === 0;
  }
});

btnExplain.addEventListener("click", async () => {
  const modeInput = document.querySelector('input[name="report-mode"]:checked');
  const mode = modeInput ? modeInput.value : "B";

  console.log("[4단계] AI 보고서 생성 시작 (모드):", mode);
  setBusy(true, `처리 중... 모드 ${mode} 보고서를 생성하고 있습니다.`);
  try {
    const resp = await fetch("/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "보고서 생성 중 오류가 발생했습니다.");
    }
    reportOutput.value = data.report || "";
    setPreviewMode(false);
    syncReportButtons();
    console.log("[4단계] 보고서 생성 완료 (길이):", reportOutput.value.length);
    showBanner(`모드 ${mode} 보고서 생성이 완료되었습니다. 필요하면 직접 수정하거나 미리보기를 눌러 보세요.`, "success");
  } catch (err) {
    console.error("[4단계] 오류:", err);
    showBanner(err.message, "error");
  } finally {
    setBusy(false);
    btnRun.disabled = currentSuggestions.length === 0;
  }
});

reportOutput.addEventListener("input", () => {
  syncReportButtons();
});

btnPreview.addEventListener("click", () => {
  setPreviewMode(!isPreviewMode);
  console.log("[보고서] 미리보기 전환:", isPreviewMode);
});

btnCopy.addEventListener("click", async () => {
  if (!reportOutput.value) return;
  try {
    await navigator.clipboard.writeText(reportOutput.value);
    console.log("[5단계] 보고서 클립보드 복사 완료");
    showBanner("보고서가 클립보드에 복사되었습니다.", "success");
  } catch (err) {
    console.error("복사 실패:", err);
    showBanner("복사에 실패했습니다. 보고서 창에서 직접 Ctrl+C로 복사해 주세요.", "error");
  }
});

btnDownload.addEventListener("click", () => {
  if (!reportOutput.value) return;
  const modeInput = document.querySelector('input[name="report-mode"]:checked');
  const mode = modeInput ? modeInput.value : "B";
  const blob = new Blob([reportOutput.value], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `course_report_mode${mode}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  console.log("[5단계] 보고서 .md 다운로드 완료:", a.download);
});

checkAuthStatus();
