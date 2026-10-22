/* ==========================================================================
   KIC SignalForge — 브라우저 UI
   키 등 비밀값은 이 파일에 없다. 모든 요청은 서버 /api/* 를 경유한다.
   ========================================================================== */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const el = {
    messages: $("messages"),
    examples: $("examples"),
    exampleChips: $("example-chips"),
    composer: $("composer"),
    composerInput: $("composer-input"),
    composerLabel: $("composer-label"),
    composerHint: $("composer-hint"),
    sendBtn: $("send-btn"),
    advanced: $("advanced"),
    advancedToggle: $("advanced-toggle"),
    advancedRun: $("advanced-run"),
    statusChip: $("status-chip"),
    themeToggle: $("theme-toggle"),
    newChat: $("new-chat"),
    connectorList: $("connector-list"),
    recentList: $("recent-list"),
    chatHead: $("chat-head"),
  };

  const EXAMPLES = [
    "2025년 삼성전자 반도체 실적 전망 근거는?",
    "원/달러 환율이 국내 수출 기업에 미치는 영향은?",
    "한국은행 기준금리 인상 시나리오의 근거는?",
    "국내 반도체 산업생산지수 흐름을 알려줘",
  ];

  const state = {
    sessionId: null,
    step: "welcome",
    busy: false,
    theme: "light",
  };

  /* ---------------------------------------------------------------- utils */
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function num(value, digits) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("ko-KR", {
      minimumFractionDigits: digits === undefined ? 0 : digits,
      maximumFractionDigits: digits === undefined ? 0 : digits,
    });
  }

  function pct(value, digits) {
    if (value === null || value === undefined) return "—";
    return (Number(value) * 100).toFixed(digits === undefined ? 0 : digits) + "%";
  }

  function signed(value, digits) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    return (n >= 0 ? "+" : "") + n.toFixed(digits === undefined ? 2 : digits) + "%";
  }

  function debounce(fn, wait) {
    let timer = null;
    return function () {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  /* -------------------------------------------------------------- messages */
  function addMessage(role, html, options) {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-" + role;
    const avatar = role === "assistant" ? "SF" : "나";
    wrap.innerHTML =
      '<div class="msg-avatar">' + esc(avatar) + "</div>" +
      '<div class="msg-body"><div class="bubble">' + html + "</div></div>";
    el.messages.appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  function addResultBlock(html) {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-assistant";
    wrap.innerHTML = '<div class="msg-avatar">SF</div><div class="msg-body">' + html + "</div>";
    el.messages.appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    });
  }

  function setBusy(busy) {
    state.busy = busy;
    el.sendBtn.disabled = busy;
    el.advancedRun.disabled = busy;
    el.sendBtn.innerHTML = busy ? '<span class="spinner"></span> 조사 중' : "보내기";
    if (!busy) updateComposerPrompt(state.step);
  }

  /* --------------------------------------------------------------- health */
  async function loadHealth() {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      const conns = data.connectors || {};
      const live = Object.values(conns).filter((c) => c.mode === "live").length;
      const sample = Object.values(conns).filter((c) => c.mode === "sample").length;
      el.statusChip.textContent = live + "개 실제 · " + sample + "개 샘플";
      el.statusChip.className = "status-chip " + (live > 0 ? "is-ok" : "is-warn");

      el.connectorList.innerHTML = Object.values(conns)
        .map((c) => {
          const mode =
            c.mode === "live"
              ? '<span class="chip is-success">실제 API</span>'
              : c.mode === "sample"
              ? '<span class="chip is-warning">샘플 키</span>'
              : '<span class="chip">사용 불가</span>';
          return (
            "<li><span>" + esc(c.label || "") + "</span>" + mode + "</li>"
          );
        })
        .join("");
    } catch (err) {
      el.statusChip.textContent = "상태 확인 실패";
      el.statusChip.className = "status-chip";
    }
  }

  async function loadRecent() {
    try {
      const res = await fetch("/api/runs?limit=5");
      const data = await res.json();
      const runs = data.runs || [];
      if (!runs.length) {
        el.recentList.innerHTML = '<li class="rail-empty">아직 저장된 브리핑이 없습니다.</li>';
        return;
      }
      el.recentList.innerHTML = runs
        .map(
          (r) =>
            '<li><a href="/api/runs/' +
            encodeURIComponent(r.run_id) +
            '" target="_blank" rel="noopener">' +
            esc((r.question || "").slice(0, 34)) +
            "</a><span>" +
            esc((r.created_at || "").slice(5, 10)) +
            "</span></li>"
        )
        .join("");
    } catch (err) {
      /* 목록 실패는 무시 */
    }
  }

  /* ----------------------------------------------------------------- chat */
  async function sendMessage(text) {
    if (state.busy) return;
    const message = (text || "").trim();
    if (!message) return;

    state.busy = true;
    el.composerInput.value = "";
    autoGrow();
    addMessage("user", esc(message));

    const thinking = addMessage(
      "assistant",
      '<span class="spinner"></span> <span class="muted">공개 자료를 확인하고 있습니다…</span>'
    );

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, message: message }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "요청 실패");
      state.sessionId = data.session_id;
      try { localStorage.setItem("sf-session", state.sessionId); } catch (err) { /* 저장 실패는 무시 */ }
      state.step = data.step;
      thinking.remove();

      addMessage("assistant", renderReply(data.reply));
      renderQuickReplies(data.quick_replies || []);

      if (data.state && data.state.result) {
        renderResult(data.state.result);
        loadRecent();
      }
      updateComposerPrompt(data.step);
    } catch (err) {
      thinking.remove();
      addMessage("assistant", "조사 중 오류가 발생했습니다: " + esc(err.message || "알 수 없는 오류"));
    } finally {
      state.busy = false;
      setBusy(false);
    }
  }

  function renderReply(text) {
    const escaped = esc(text);
    return escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  function updateComposerPrompt(step) {
    const map = {
      welcome: "무엇이 궁금하신가요?",
      question: "무엇이 궁금하신가요?",
      balance: "투자 잔고는 얼마로 설정할까요?",
      running: "조사 중입니다…",
      done: "새로 물어볼 내용을 입력하세요",
    };
    el.composerLabel.textContent = map[step] || "무엇이 궁금하신가요?";
    el.composerInput.placeholder =
      step === "balance" ? "예) 1,000만원" : "예) 2025년 삼성전자 반도체 실적 전망 근거는?";
    el.sendBtn.textContent = step === "balance" ? "완료하고 분석" : "보내기";
  }

  function renderQuickReplies(items) {
    if (!items || !items.length) {
      el.examples.hidden = true;
      return;
    }
    el.examples.hidden = false;
    el.exampleChips.innerHTML = items
      .map(
        (t) =>
          '<button type="button" class="chip-btn" data-q="' + esc(t) + '">' + esc(t) + "</button>"
      )
      .join("");
  }

  /* --------------------------------------------------------------- result */
  function renderResult(result) {
    const briefing = result.briefing || {};
    const simulation = result.simulation || null;
    const outlook = briefing.outlook || null;
    window.__SIM__ = simulation;

    let html = '<div class="result">';
    html += renderOutlookCard(outlook, briefing);
    if (simulation) html += renderSimulationCard(simulation);
    html += renderClaimsCard(briefing);
    html += renderSourcesCard(briefing);
    html += renderWarningsCard(briefing);
    html += renderReportCard(briefing);
    html += "</div>";
    addResultBlock(html);
    bindChart();
  }

  function renderOutlookCard(outlook, briefing) {
    if (!outlook) {
      return (
        '<section class="card"><h3 class="card-title">전망</h3>' +
        '<p class="card-note">확인 가능한 근거가 없어 전망을 산출하지 않았습니다.</p></section>'
      );
    }
    const up = outlook.probability_up || 0;
    const down = outlook.probability_down || 0;
    const ret = outlook.expected_return || 0;
    const retClass = ret > 0 ? "is-up" : ret < 0 ? "is-down" : "is-flat";
    const conf = Math.round((outlook.confidence || 0) * 100);

    const evidence = (list, cls) =>
      list && list.length
        ? list
            .map(
              (e) =>
                '<li><span class="' +
                cls +
                '">' +
                esc(e) +
                "</span></li>"
            )
            .join("")
        : '<li class="ev-empty">해당 방향의 근거가 확인되지 않았습니다.</li>';

    return (
      '<section class="card">' +
      '<div class="card-head"><div><p class="overline">AI OUTLOOK</p>' +
      '<h3 class="card-title">상승·하락 전망</h3></div>' +
      '<span class="chip ' +
      (up >= 0.5 ? "is-success" : "is-error") +
      '">근거 신뢰도 ' +
      conf +
      "%</span></div>" +
      '<div class="stat-grid">' +
      '<div class="stat is-up"><p class="stat-label">상승 확률</p>' +
      '<p class="stat-value">' +
      pct(up) +
      '</p><p class="stat-sub">' +
      (outlook.bull_evidence || []).length +
      "건의 상승 근거</p></div>" +
      '<div class="stat is-down"><p class="stat-label">하락 확률</p>' +
      '<p class="stat-value">' +
      pct(down) +
      '</p><p class="stat-sub">' +
      (outlook.bear_evidence || []).length +
      "건의 하락 근거</p></div>" +
      '<div class="stat ' +
      retClass +
      '"><p class="stat-label">기대수익률</p>' +
      '<p class="stat-value">' +
      signed(ret) +
      '</p><p class="stat-sub">12개월 · 근거 기반 추정</p></div>' +
      "</div>" +
      '<div class="bar"><span class="bar-up" style="width:' +
      (up * 100).toFixed(1) +
      '%"></span><span class="bar-down" style="width:' +
      (down * 100).toFixed(1) +
      '%"></span></div>' +
      '<div class="evidence-grid" style="margin-top:var(--sp-5)">' +
      '<div class="ev-col is-bull"><p class="ev-col-head">상승 근거</p><ul class="ev-list">' +
      evidence(outlook.bull_evidence, "") +
      "</ul></div>" +
      '<div class="ev-col is-bear"><p class="ev-col-head">하락 근거</p><ul class="ev-list">' +
      evidence(outlook.bear_evidence, "") +
      "</ul></div>" +
      "</div>" +
      '<div class="divider"></div>' +
      '<dl class="kv-list">' +
      (outlook.notes || [])
        .map(
          (n) =>
            '<div class="kv"><dt>' +
            esc(n) +
            "</dt><dd></dd></div>"
        )
        .join("") +
      "</dl>" +
      '<p class="card-note" style="margin-top:var(--sp-3)">' +
      esc(outlook.method || "") +
      "</p>" +
      '<p class="card-note"><strong>AI 추정치이며 투자 권유가 아닙니다.</strong> 최종 판단은 사용자 검토가 필요합니다.</p>' +
      "</section>"
    );
  }

  function renderSimulationCard(sim) {
    const scenarios = sim.scenarios || [];
    const legend = scenarios
      .map((s) => {
        // 기준 시나리오는 확률이 아니라 '기대 경로'이므로 확률을 붙이지 않는다.
        const prob = s.key === "base" ? "" : " " + pct(s.probability);
        return (
          '<span class="legend-item"><span class="legend-swatch" style="background:' +
          s.color +
          '"></span>' +
          esc(s.label) +
          prob +
          "</span>"
        );
      })
      .join("");

    const balanceRow =
      sim.balance !== null && sim.balance !== undefined
        ? '<div class="kv"><dt>설정 잔고</dt><dd>' + num(sim.balance) + "원</dd></div>"
        : "";
    const balanceOutcomes = sim.balance !== null && sim.balance !== undefined
      ? '<div class="balance-outcomes">' + scenarios.map((s) => {
          const estimated = Number(sim.balance) * (1 + Number(s.return_pct || 0) / 100);
          const delta = estimated - Number(sim.balance);
          return '<article class="balance-outcome"><span class="chip" style="--scenario-color:' + esc(s.color || "var(--primary)") + '">' + esc(s.label) + (s.key === "base" ? "" : " · " + pct(s.probability)) + '</span><strong>' + num(estimated) + '원</strong><small class="' + (delta >= 0 ? "is-up" : "is-down") + '">' + (delta >= 0 ? "+" : "") + num(delta) + '원 (' + signed(s.return_pct) + ')</small></article>';
        }).join("") + "</div>" : "";

    return (
      '<section class="card" id="sim-card">' +
      '<div class="card-head"><div><p class="overline">SIMULATION</p>' +
      '<h3 class="card-title">주식 시뮬레이션</h3></div>' +
      '<span class="chip is-primary">' +
      esc(sim.symbol || "") +
      "</span></div>" +
      '<p class="card-note">과거는 실제 종가, 미래는 근거 기반 3개 시나리오입니다. ' +
      "차트의 점에 마우스를 올리면 그 시점의 전망과 근거를 볼 수 있습니다.</p>" +
      '<dl class="kv-list">' +
      '<div class="kv"><dt>종목</dt><dd>' +
      esc(sim.company || sim.symbol || "—") +
      "</dd></div>" +
      '<div class="kv"><dt>현재가</dt><dd>' +
      num(sim.current_price) +
      " " +
      esc(sim.currency || "KRW") +
      "</dd></div>" +
      balanceRow +
      "</dl>" +
      balanceOutcomes +
      '<dl class="kv-list">' +
      '<div class="kv"><dt>데이터 출처</dt><dd>' +
      esc(sim.data_source || "—") +
      "</dd></div>" +
      "</dl>" +
      '<div class="chart-wrap" id="chart-wrap">' +
      '<svg class="chart" id="sim-chart" viewBox="0 0 900 340" role="img" aria-label="주가 시뮬레이션 차트"></svg>' +
      '<div class="chart-tooltip" id="chart-tooltip"></div>' +
      "</div>" +
      '<div class="chart-legend">' +
      legend +
      '<span class="legend-item"><span class="legend-swatch" style="background:var(--primary)"></span>과거 실제 종가</span>' +
      "</div>" +
      '<div class="btn-actions">' +
      scenarios
        .map(
          (s) =>
            '<span class="chip">' + esc(s.label) + " · 최종 " + num(s.final_price) + " (" + signed(s.return_pct) + ")</span>"
        )
        .join("") +
      "</div>" +
      '<p class="card-note" style="margin-top:var(--sp-3)">' +
      (sim.notes || []).map(esc).join(" ") +
      "</p>" +
      '<p class="card-note">' + esc(sim.disclaimer || "") + "</p>" +
      "</section>"
    );
  }

  function renderClaimsCard(briefing) {
    const claims = briefing.claims || [];
    const supported = claims.filter((c) => c.status === "supported");
    const contested = claims.filter((c) => c.status === "contested");
    const unverified = claims.filter((c) => c.status === "unverified");
    const ordered = supported.concat(contested, unverified).slice(0, 12);

    const items = ordered
      .map((c) => {
        const statusChip =
          c.status === "supported"
            ? '<span class="chip is-success">근거 있음</span>'
            : c.status === "contested"
            ? '<span class="chip is-warning">상충 근거 있음</span>'
            : '<span class="chip is-error">근거 부족</span>';
        const metrics = (c.metrics || [])
          .slice(0, 4)
          .map(
            (m) =>
              esc(m.label) +
              ": <b>" +
              esc(m.raw_value) +
              (m.unit ? " " + esc(m.unit) : "") +
              (m.period ? " (" + esc(m.period) + ")" : "") +
              "</b> · <code>" +
              esc(m.source_id || "-") +
              "</code>"
          )
          .join(" · ");
        const quotes = (c.quotes || [])
          .filter((q) => q.verified)
          .slice(0, 1)
          .map((q) => '<p class="quote">"' + esc(q.text.slice(0, 260)) + '"</p>')
          .join("");
        return (
          '<article class="claim is-' +
          c.status +
          '"><div class="claim-head"><span class="claim-id">' +
          esc(c.claim_id) +
          "</span>" +
          statusChip +
          "</div>" +
          '<p class="claim-text">' +
          esc(c.text) +
          "</p>" +
          '<div class="claim-meta">' +
          metrics +
          "</div>" +
          quotes +
          "</article>"
        );
      })
      .join("");

    return (
      '<section class="card"><p class="overline">CLAIMS</p>' +
      '<h3 class="card-title">주장별 근거 · 반대 근거</h3>' +
      '<p class="card-note">근거 ' +
      supported.length +
      "건 · 상충 " +
      contested.length +
      "건 · 확인 불가 " +
      unverified.length +
      "건</p>" +
      (items || '<p class="card-note">확인 가능한 주장이 없습니다.</p>') +
      "</section>"
    );
  }

  function renderSourcesCard(briefing) {
    const sources = briefing.sources || [];
    const rows = sources
      .map(
        (s) =>
          "<tr><td><span class=\"source-id\">" +
          esc(s.source_id) +
          "</span></td>" +
          '<td><a class="source-title" href="' +
          esc(s.url) +
          '" target="_blank" rel="noopener noreferrer">' +
          esc(s.title) +
          "</a></td>" +
          "<td>" +
          esc(s.published_at || "미확인") +
          "</td><td>" +
          esc(s.period_label || "미확인") +
          "</td><td>" +
          esc(s.unit || "미확인") +
          '</td><td><span class="chip ' +
          (s.provenance === "live" ? "is-success" : "is-warning") +
          '">' +
          esc(s.provenance_label || s.provenance) +
          "</span></td></tr>"
      )
      .join("");

    return (
      '<section class="card" id="sources-card"><p class="overline">SOURCES</p>' +
      '<h3 class="card-title">자료 출처</h3>' +
      '<p class="card-note">발행일 · 기준기간 · 단위를 함께 표시합니다. 링크를 누르면 원문으로 이동합니다.</p>' +
      (rows
        ? '<div style="overflow-x:auto"><table class="source-table"><thead><tr>' +
          "<th>출처ID</th><th>자료</th><th>발행일</th><th>기준기간</th><th>단위</th><th>구분</th>" +
          "</tr></thead><tbody>" +
          rows +
          "</tbody></table></div>"
        : '<p class="card-note">수집된 출처가 없습니다.</p>') +
      "</section>"
    );
  }

  function renderWarningsCard(briefing) {
    const warnings = briefing.uncertainties || [];
    const errors = briefing.errors || [];
    const uniqueErrors = Array.from(
      new Map(errors.map((e) => [`${e.provider}|${e.code}|${e.message}`, e])).values()
    );
    const errorGapKeys = new Set(
      uniqueErrors.map((e) => `${e.provider}: ${e.message}`)
    );
    const gapItems = Array.from(new Set(briefing.data_gaps || []))
      .filter((gap) => !Array.from(errorGapKeys).some((key) => gap.includes(key)))
      .slice(0, 6);

    let html =
      '<section class="card"><p class="overline">UNCERTAINTY</p>' +
      '<h3 class="card-title">불확실성 · 자료 공백 · 수집 상태</h3>' +
      '<p class="card-note">' +
      warnings.length +
      "건의 자료 경고</p>";

    if (warnings.length) {
      html +=
        '<ul class="warn-list">' +
        warnings
          .map(
            (w) =>
              '<li class="sev-' +
              esc(w.severity) +
              '"><span class="warn-kind">' +
              esc(w.kind) +
              "</span><span>" +
              esc(w.message) +
              "</span></li>"
          )
          .join("") +
        "</ul>";
    } else {
      html += '<p class="card-note">자료 상충이나 최신성 경고가 확인되지 않았습니다. 근거가 충분하다는 뜻은 아닙니다.</p>';
    }

    if (uniqueErrors.length) {
      html += '<div class="divider"></div><p class="overline">자료 수집 실패</p><ul class="warn-list">';
      html += uniqueErrors
        .map(
          (e) =>
            '<li class="sev-critical"><span class="warn-kind">' +
            esc(e.provider) +
            " · " +
            esc(e.code) +
            "</span><span>" +
            esc(e.message) +
            "</span></li>"
        )
        .join("");
      html += "</ul>";
    }

    if (gapItems.length) {
      html += '<div class="divider"></div><p class="overline">DATA GAPS</p><ul class="warn-list">';
      html += gapItems
        .map((g) => '<li><span class="warn-kind">미확인</span><span>' + esc(g) + "</span></li>")
        .join("");
      html += "</ul>";
    }

    return html + "</section>";
  }

  function renderReportCard(briefing) {
    const md = briefing.report_markdown || "";
    return (
      '<section class="card"><p class="overline">BRIEFING</p>' +
      '<h3 class="card-title">브리핑 원문</h3>' +
      '<p class="card-note">출처 링크가 포함된 Markdown 브리핑입니다.</p>' +
      '<div class="btn-actions">' +
      '<button type="button" class="btn-secondary btn-sm" id="copy-btn">복사</button>' +
      '<button type="button" class="btn-secondary btn-sm" id="download-btn">다운로드(.md)</button>' +
      "</div>" +
      '<pre class="quote" style="max-height:320px;overflow:auto;margin-top:var(--sp-4);white-space:pre-wrap">' +
      esc(md.slice(0, 12000)) +
      "</pre></section>"
    );
  }

  /* ---------------------------------------------------------------- chart */
  function bindChart() {
    const svg = $("sim-chart");
    const tooltip = $("chart-tooltip");
    if (!svg || !tooltip) return;
    const payload = window.__SIM__;
    if (!payload) return;

    const all = payload.history || [];
    const scenarios = payload.scenarios || [];
    const points = payload.outlook_points || [];
    if (!all.length) return;
    // 과거는 최근 120거래일만 그려 미래 구간(12개월)이 충분한 폭을 갖게 한다.
    const history = all.slice(-120);

    const W = 900;
    const H = 340;
    const padL = 56;
    const padR = 16;
    const padT = 16;
    const padB = 30;

    const historyX = history.map((p, i) => i);
    const forecastLen = scenarios.length ? scenarios[0].points.length : 0;
    const totalSlots = history.length + forecastLen;

    let min = Infinity;
    let max = -Infinity;
    const consider = (v) => {
      if (v === null || v === undefined) return;
      if (v < min) min = v;
      if (v > max) max = v;
    };
    history.forEach((p) => consider(p.close));
    scenarios.forEach((s) => s.points.forEach((p) => consider(p.close)));
    if (!isFinite(min) || !isFinite(max)) return;
    const span = max - min || Math.max(1, max * 0.05);
    min -= span * 0.08;
    max += span * 0.08;

    const x = (slot) =>
      padL + (totalSlots <= 1 ? 0 : (slot / (totalSlots - 1)) * (W - padL - padR));
    const y = (v) => padT + (1 - (v - min) / (max - min)) * (H - padT - padB);

    const parts = [];

    // 그리드 + Y축
    parts.push('<g class="chart-grid">');
    for (let i = 0; i <= 4; i++) {
      const v = min + ((max - min) * i) / 4;
      const yy = y(v);
      parts.push('<line x1="' + padL + '" y1="' + yy + '" x2="' + (W - padR) + '" y2="' + yy + '"/>');
      parts.push(
        '<text class="chart-axis" x="' + (padL - 8) + '" y="' + (yy + 3) + '" text-anchor="end">' +
          num(v) +
          "</text>"
      );
    }
    parts.push("</g>");

    // 과거 실제 종가
    const histPath = history
      .map((p, i) => (i === 0 ? "M" : "L") + x(i).toFixed(1) + " " + y(p.close).toFixed(1))
      .join(" ");
    parts.push('<path class="chart-line is-history" d="' + histPath + '"/>');

    // 미래 시나리오
    const historyLastX = x(history.length - 1);
    const historyLastY = y(history[history.length - 1].close);
    scenarios.forEach((s) => {
      if (!s.points || !s.points.length) return;
      const d =
        "M" + historyLastX.toFixed(1) + " " + historyLastY.toFixed(1) +
        s.points
          .map(
            (p, i) =>
              " L" + x(history.length + i).toFixed(1) + " " + y(p.close).toFixed(1)
          )
          .join("");
      parts.push('<path class="chart-line is-forecast" d="' + d + '" stroke="' + s.color + '"/>');
    });

    // 히트 영역(hover)
    points.forEach((p, i) => {
      const slot = history.length + i;
      parts.push(
        '<circle class="chart-hit" data-index="' + i + '" cx="' + x(slot).toFixed(1) + '" cy="' +
          y(p.price).toFixed(1) + '" r="14"></circle>'
      );
    });

    // X축 라벨
    const labelStep = Math.max(1, Math.floor(history.length / 6));
    for (let i = 0; i < history.length; i += labelStep) {
      parts.push(
        '<text class="chart-axis" x="' + x(i).toFixed(1) + '" y="' + (H - 10) + '" text-anchor="middle">' +
          esc(history[i].date.slice(2)) +
          "</text>"
      );
    }
    points.forEach((p, i) => {
      if (i % 4 === 0) {
        parts.push(
          '<text class="chart-axis" x="' + x(history.length + i).toFixed(1) + '" y="' + (H - 10) +
            '" text-anchor="middle">' + esc(p.date) + "</text>"
        );
      }
    });

    svg.innerHTML = parts.join("");

    // hover
    svg.querySelectorAll(".chart-hit").forEach((node) => {
      node.addEventListener("mouseenter", () => {
        const idx = Number(node.getAttribute("data-index"));
        const p = points[idx];
        if (!p) return;
        tooltip.innerHTML =
          '<p class="tt-title">' +
          esc(p.date) +
          " · " +
          num(p.price) +
          "</p>" +
          '<div class="tt-row"><span>누적 수익률</span><b>' +
          signed(p.cumulative_return) +
          "</b></div>" +
          '<div class="tt-row"><span>전망</span><b>' +
          esc(p.outlook) +
          "</b></div>" +
          (p.detail ? '<div class="tt-row"><span>' + esc(p.detail) + "</span></div>" : "") +
          (p.evidence && p.evidence.length
            ? '<div class="tt-evidence">' +
              p.evidence.map((e) => "· " + esc(e)).join("<br />") +
              "</div>"
            : "");
        const box = node.getBoundingClientRect();
        const wrapBox = tooltip.parentElement.getBoundingClientRect();
        let left = box.left - wrapBox.left + box.width / 2 - 120;
        left = Math.max(4, Math.min(left, wrapBox.width - 330));
        tooltip.style.left = left + "px";
        tooltip.style.top = Math.max(4, box.top - wrapBox.top - 12) + "px";
        tooltip.classList.add("is-visible");
      });
      node.addEventListener("mouseleave", () => tooltip.classList.remove("is-visible"));
    });
  }

  /* ------------------------------------------------------------- advanced */
  function runAdvanced() {
    const question = ($("adv-question").value || "").trim();
    if (!question) {
      $("adv-question").focus();
      return;
    }
    const payload = {
      question: question,
      company: ($("adv-company").value || "").trim(),
      topic: ($("adv-topic").value || "").trim(),
      period: ($("adv-period").value || "").trim(),
      balance: ($("adv-balance").value || "").trim(),
      ir_urls: ($("adv-ir").value || "").trim(),
    };
    runDirect(payload);
  }

  async function runDirect(payload) {
    if (state.busy) return;
    state.busy = true;
    setBusy(true);
    addMessage(
      "user",
      "<strong>직접 입력</strong><br />" + esc(payload.question) +
        (payload.company ? "<br />기업: " + esc(payload.company) : "") +
        (payload.period ? "<br />기간: " + esc(payload.period) : "") +
        (payload.balance ? "<br />잔고: " + esc(payload.balance) : "")
    );
    const thinking = addMessage(
      "assistant",
      '<span class="spinner"></span> <span class="muted">공개 자료를 확인하고 있습니다…</span>'
    );
    try {
      const body = {
        question: payload.question,
        company: payload.company,
        topic: payload.topic,
        period: payload.period,
      };
      const ir = (payload.ir_urls || "").trim();
      if (ir) body.ir_urls = [ir];
      const res = await fetch("/api/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "요청 실패");
      thinking.remove();

      const balance = parseBalance(payload.balance);
      if (balance) {
        const sim = await fetchSimulation(data, balance);
        if (sim) data.simulation = sim;
      }
      window.__SIM__ = data.simulation || null;
      renderResult(data);
      loadRecent();
    } catch (err) {
      thinking.remove();
      addMessage("assistant", "조사 중 오류가 발생했습니다: " + esc(err.message || "알 수 없는 오류"));
    } finally {
      state.busy = false;
      setBusy(false);
    }
  }

  async function fetchSimulation(briefing, balance) {
    try {
      const res = await fetch("/api/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company: briefing.company || "",
          question: briefing.question,
          balance: balance,
        }),
      });
      if (!res.ok) return null;
      const data = await res.json();
      return data.simulation || null;
    } catch (err) {
      return null;
    }
  }

  function parseBalance(text) {
    if (!text) return null;
    const m = String(text).replace(/ /g, "").match(/([\d,]+)(천만|만|억|조)?/);
    if (!m) return null;
    const value = parseFloat(m[1].replace(/,/g, ""));
    const mult = { "": 1, "만": 10000, "천만": 10000000, "억": 100000000, "조": 100000000000 }[m[2] || ""];
    return value * mult;
  }

  /* ---------------------------------------------------------------- theme */
  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("sf-theme", theme);
    } catch (err) {
      /* 저장 실패는 무시 */
    }
  }

  /* ----------------------------------------------------------------- init */
  function autoGrow() {
    el.composerInput.style.height = "auto";
    el.composerInput.style.height = Math.min(200, el.composerInput.scrollHeight) + "px";
  }

  function init() {
    try {
      const saved = localStorage.getItem("sf-theme");
      if (saved) applyTheme(saved);
    } catch (err) {
      /* 무시 */
    }

    renderQuickReplies(EXAMPLES);
    loadHealth();
    loadRecent();

    el.composer.addEventListener("submit", (e) => {
      e.preventDefault();
      sendMessage(el.composerInput.value);
    });

    el.composerInput.addEventListener("input", autoGrow);
    el.composerInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage(el.composerInput.value);
      }
    });

    el.exampleChips.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-q]");
      if (!btn) return;
      const value = btn.getAttribute("data-q");
      if (value === "새 질문 하기") startNewChat();
      else if (value === "브리핑 다운로드") downloadReport();
      else sendMessage(value);
    });

    el.advancedToggle.addEventListener("click", () => {
      const open = el.advanced.hidden;
      el.advanced.hidden = !open;
      el.advancedToggle.setAttribute("aria-expanded", String(open));
      el.advancedToggle.textContent = open ? "직접 입력 닫기" : "직접 입력으로 조사";
    });
    el.advancedRun.addEventListener("click", runAdvanced);

    el.themeToggle.addEventListener("click", () => {
      applyTheme(state.theme === "light" ? "dark" : "light");
    });

    document.addEventListener("click", (e) => {
      const copy = e.target.closest("#copy-btn");
      const dl = e.target.closest("#download-btn");
      if (copy) copyReport();
      if (dl) downloadReport();
    });

    // 새로 들어오면 서버 세션을 만들고 환영 메시지를 다시 보여준다.
    const params = new URLSearchParams(location.search);
    const restored = params.get("session");
    if (restored) {
      state.sessionId = restored;
      restoreSession(restored);
    } else startNewChat();

    el.newChat.addEventListener("click", startNewChat);
  }

  async function startNewChat() {
    if (state.busy) return;
    el.messages.innerHTML = "";
    state.sessionId = null;
    state.step = "welcome";
    try { localStorage.removeItem("sf-session"); } catch (err) { /* 무시 */ }
    try {
      const res = await fetch("/api/chat/reset", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: null, message: "" }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "요청 실패");
      state.sessionId = data.session_id;
      state.step = data.step || "welcome";
      try { localStorage.setItem("sf-session", state.sessionId); } catch (err) { /* 무시 */ }
      addMessage("assistant", renderReply(data.reply));
      renderQuickReplies(data.quick_replies || EXAMPLES);
      updateComposerPrompt(state.step);
    } catch (err) {
      addMessage("assistant", "환영합니다. <strong>무엇이 궁금하신가요?</strong>");
      renderQuickReplies(EXAMPLES);
    }
  }

  async function restoreSession(sessionId) {
    try {
      const res = await fetch("/api/chat/state?session_id=" + encodeURIComponent(sessionId));
      if (!res.ok) { await startNewChat(); return; }
      const data = await res.json();
      state.step = data.step || state.step;
      try { localStorage.setItem("sf-session", sessionId); } catch (err) { /* 무시 */ }
      (data.messages || []).forEach((m) => {
        if (m.role === "user") addMessage("user", esc(m.text));
        else addMessage("assistant", renderReply(m.text));
      });
      if (data.result) renderResult(data.result);
      updateComposerPrompt(state.step);
    } catch (err) {
      /* 복원 실패는 무시하고 새로 시작 */
    }
  }

  function copyReport() {
    const pre = document.querySelector("#sim-card ~ .card pre.quote") ||
      Array.from(document.querySelectorAll("pre.quote")).pop();
    if (!pre) return;
    const text = pre.textContent || "";
    navigator.clipboard
      .writeText(text)
      .then(() => alert("브리핑을 복사했습니다."))
      .catch(() => alert("복사에 실패했습니다."));
  }

  function downloadReport() {
    const pre = Array.from(document.querySelectorAll("pre.quote")).pop();
    if (!pre) return;
    const blob = new Blob([pre.textContent || ""], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "signalforge-briefing.md";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  init();
})();
