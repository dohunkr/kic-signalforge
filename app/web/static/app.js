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
    question: "",
    lastRequest: null,
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
    document.querySelectorAll(".msg-result").forEach((node) => node.remove());
    const wrap = document.createElement("div");
    wrap.className = "msg msg-assistant msg-result";
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

    // 질문을 받을 때 서버에서 기업 목록(DART ZIP)을 내려받지 않는다.
    // Vercel은 요청마다 다른 인스턴스로 연결될 수 있으므로 질문/잔고 단계는
    // 브라우저에서 관리하고, 두 입력이 모인 뒤 상태 없는 조사 API를 호출한다.
    if (state.step !== "balance") {
      state.question = message;
      state.step = "balance";
      el.composerInput.value = "";
      autoGrow();
      addMessage("user", esc(message));
      addMessage(
        "assistant",
        "좋습니다. 시뮬레이션에 사용할 <strong>투자 잔고</strong>는 얼마로 할까요?<br />예) 1,000만원"
      );
      renderQuickReplies(["1,000만원", "5,000만원", "1억원"]);
      updateComposerPrompt(state.step);
      return;
    }

    const balance = parseBalance(message);
    if (!balance || balance < 10000) {
      addMessage("user", esc(message));
      addMessage("assistant", "잔고를 숫자로 입력해 주세요. 예) 1,000만원 (최소 10,000원)");
      updateComposerPrompt("balance");
      return;
    }
    runDirect({ question: state.question, balance: message, conversational: true });
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
    // /api/chat returns { briefing, simulation }, while /api/research returns
    // the briefing fields at the response root. Normalize both response shapes.
    const briefing = result.briefing || (result.question ? result : {});
    const simulation = result.simulation || null;
    const outlook = briefing.outlook || null;
    window.__BRIEFING__ = briefing;
    window.__SIM__ = simulation;

    let html = '<div class="result">' + renderResultActions();
    if (briefing.changeSummary) html += renderChangeCard(briefing.changeSummary);
    html += renderAiStatusCard(briefing);
    html += renderOutlookCard(outlook, briefing);
    if (simulation) html += renderSimulationCard(simulation);
    html += renderAuditCard(briefing);
    html += renderRedTeamCard(briefing);
    html += renderWatchCard(briefing);
    html += renderClaimsCard(briefing);
    html += renderReliabilityCard(briefing);
    html += renderSourcesCard(briefing);
    html += renderWarningsCard(briefing);
    html += renderReportCard(briefing);
    html += renderFollowupsCard();
    html += "</div>";
    addResultBlock(html);
    bindChart();
    bindResultFeatures(briefing, simulation);
  }

  function renderResultActions() {
    return (
      '<div class="result-actions">' +
      '<p class="card-note">새 자료를 기준으로 결과를 갱신하거나, 이어서 질문할 수 있어요.</p>' +
      '<button type="button" class="btn-secondary btn-sm" id="rerun-analysis">최신 자료로 다시 분석</button>' +
      '<button type="button" class="btn-secondary btn-sm" id="presentation-mode">60초 심사 모드</button>' +
      '</div>'
    );
  }

  function renderAuditCard(briefing) {
    const claims = briefing.claims || [];
    const outlooks = briefing.claim_outlooks || [];
    const sources = briefing.sources || [];
    const weight = { 1: 1, 2: 1.8, 3: 2.6 };
    const totals = { up: 0, down: 0, neutral: 0 };
    outlooks.forEach((o) => { const c = claims.find((x) => x.claim_id === o.claim_id); if (c && ((c.supporting_source_ids || []).length || (c.contradicting_source_ids || []).length)) totals[o.direction] = (totals[o.direction] || 0) + (weight[o.strength] || 1); });
    const rows = outlooks.map((o) => {
      const c = claims.find((x) => x.claim_id === o.claim_id);
      if (!c) return "";
      const w = weight[o.strength] || 1;
      const ids = [...new Set([...(c.supporting_source_ids || []), ...(c.contradicting_source_ids || [])])];
      const links = sources.filter((s) => ids.includes(s.source_id));
      return '<article class="audit-row"><div><span class="chip">' + esc(o.direction || "neutral") + ' · 강도 ' + esc(o.strength || 1) + ' · 가중치 ' + w + '</span><strong>' + esc(c.text) + '</strong><p>' + esc(o.rationale || "판정 근거 설명 없음") + '</p>' +
        (links.length ? '<div class="audit-links">' + links.slice(0, 3).map((s) => '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.title) + ' ↗</a>').join("") + '</div>' : '<small>연결된 출처 없음 — 확률 반영에서 제외될 수 있음</small>') + '</div></article>';
    }).filter(Boolean).join("");
    return '<section class="card audit-card"><p class="overline">REPRODUCIBLE ANALYSIS</p><h3 class="card-title">전망을 계산한 근거</h3><p class="card-note">방향 판정에 사용한 주장과 강도별 가중치(1점·1.8점·2.6점), 원문 출처를 연결했습니다. 자료 커버리지와 불확실성은 확률을 50% 쪽으로 조정합니다.</p><div class="audit-totals"><span>상승 가중치 <strong>' + totals.up.toFixed(1) + '</strong></span><span>하락 가중치 <strong>' + totals.down.toFixed(1) + '</strong></span><span>중립 가중치 <strong>' + totals.neutral.toFixed(1) + '</strong></span><span>커버리지 <strong>' + esc(((briefing.outlook || {}).notes || []).find((n) => String(n).includes("커버리지")) || "미산출") + '</strong></span></div>' +
      (rows || '<p class="card-note">판정된 주장이 없습니다. 이 경우 확률을 신뢰도 있게 재현하기 어렵습니다.</p>') + '</section>';
  }

  function renderChangeCard(change) {
    return '<section class="card change-card"><p class="overline">UPDATE DIFF</p><h3 class="card-title">이전 분석과 달라진 점</h3><p>새로 연결된 자료 <strong>' + Number(change.added || 0) + '건</strong> · 상승 확률 변화 <strong>' + (Number(change.probabilityDelta) > 0 ? "+" : "") + Number(change.probabilityDelta || 0) + '%p</strong></p>' + ((change.titles || []).length ? '<ul>' + change.titles.map((x) => '<li>' + esc(x) + '</li>').join("") + '</ul>' : '<p class="card-note">새로 연결된 출처가 없습니다.</p>') + '</section>';
  }

  function renderAiStatusCard(briefing) {
    const usage = briefing.llm || {};
    const aiDirections = (briefing.claim_outlooks || []).filter((item) => item.method === "llm").length;
    const used = Boolean(usage.used) || aiDirections > 0;
    const fallbackReason = usage.reason || (usage.notes || []).join(" · ") ||
      "검증할 원문이 부족하거나 AI 응답이 없어 결정론적 결과를 사용했습니다.";
    const title = used
      ? (aiDirections ? "AI가 원문과 전망 근거를 분석했습니다" : "AI가 원문을 분석했습니다")
      : "AI 응답 없이 규칙 기반으로 분석했습니다";
    const detail = used
      ? (aiDirections
        ? "검증된 주장 " + aiDirections + "건의 방향을 NVIDIA NIM " + esc(usage.model || "모델") + "이 판정했습니다."
        : "NVIDIA NIM " + esc(usage.model || "모델") + "이 근거 추출에 응답했습니다. 상승·하락 방향은 규칙 기반으로 판정했습니다." + ((usage.notes || []).length ? " · " + esc(usage.notes.join(" · ")) : ""))
      : esc(fallbackReason);
    const configHint = !used && /키|인증|모델|사용 불가|호출 가능/i.test(fallbackReason)
      ? '<p class="card-note">Vercel 환경변수의 NVIDIA API 키와 모델 설정, 해당 모델의 endpoint 사용 권한을 확인해 주세요.</p>'
      : "";
    return '<section class="card ai-status-card ' + (used ? "is-working" : "is-fallback") + '"><p class="overline">AI CONNECTION</p><h3 class="card-title">' + title + '</h3><p class="card-note">' + detail + '</p>' + configHint + '</section>';
  }

  function renderRedTeamCard(briefing) {
    return '<section class="card redteam-card"><p class="overline">ADVERSARIAL REVIEW</p><h3 class="card-title">반대편 AI 심사</h3><p class="card-note">현재 전망을 반박할 가장 강한 근거를 같은 AI 모델에 별도 요청합니다. 다른 모델을 연결하지 않은 경우 독립 모델 비교는 아닙니다.</p><button class="btn-secondary btn-sm" id="run-red-team" type="button">반대 심사 실행</button><div id="red-team-output" aria-live="polite"></div></section>';
  }

  function renderWatchCard(briefing) {
    const active = localStorage.getItem("sf-watch-run") === String(briefing.run_id);
    return '<section class="card watch-card"><p class="overline">SOURCE WATCH</p><h3 class="card-title">자료 변화 알림</h3><p class="card-note">브라우저가 열려 있는 동안 24시간 뒤 최신 자료 확인 알림을 띄웁니다. 알림에서 다시 분석하면 새로 연결된 출처와 이전 결과를 비교할 수 있습니다.</p><button class="btn-secondary btn-sm" id="toggle-watch" type="button">' + (active ? '알림 끄기' : '24시간 자료 확인 알림 켜기') + '</button><span id="watch-status" class="watch-status">' + (active ? '이 분석에 대한 확인 알림이 켜져 있습니다.' : '') + '</span></section>';
  }

  function bindResultFeatures(briefing, simulation) {
    const sliders = Array.from(document.querySelectorAll("[data-assumption]"));
    if (sliders.length && simulation && simulation.balance) {
      const baseUp = Number((briefing.outlook || {}).probability_up || 0.5);
      const baseRet = Number((briefing.outlook || {}).expected_return || 0);
      const calc = () => {
        const vals = Object.fromEntries(sliders.map((s) => [s.dataset.assumption, Number(s.value)]));
        document.querySelectorAll("[data-value]").forEach((o) => { const s = sliders.find((x) => x.dataset.assumption === o.dataset.value); o.value = s.value + (o.dataset.value === "rate" ? "%p" : "%"); o.textContent = o.value; });
        const delta = -1.5 * vals.rate + 0.25 * vals.fx + 0.2 * vals.chip;
        const adjustedUp = Math.max(0.05, Math.min(0.95, baseUp + delta * 0.03));
        const adjustedReturn = baseRet + delta;
        document.querySelectorAll(".balance-outcome").forEach((card) => {
          const key = card.dataset.key;
          const original = Number(card.dataset.return || 0);
          const rate = key === "base" ? adjustedReturn : original + delta;
          const amount = Number(simulation.balance) * (1 + rate / 100);
          const diff = amount - Number(simulation.balance);
          card.querySelector(".outcome-value").textContent = num(amount) + "원";
          const deltaEl = card.querySelector(".outcome-delta");
          deltaEl.textContent = (diff >= 0 ? "+" : "") + num(diff) + "원 (" + signed(rate) + ")";
          deltaEl.className = "outcome-delta " + (diff >= 0 ? "is-up" : "is-down");
        });
        const impact = $("assumption-impact");
        if (impact) impact.textContent = "단순 민감도 참고: 상승 확률 " + pct(adjustedUp) + " · 기준 경로 " + signed(adjustedReturn) + " (기존 대비 " + signed(delta) + "%p 조정)";
      };
      sliders.forEach((s) => s.addEventListener("input", calc));
    }
    const redBtn = $("run-red-team");
    if (redBtn) redBtn.onclick = async () => {
      const out = $("red-team-output"); redBtn.disabled = true; redBtn.textContent = "반대 근거 심사 중…";
      out.innerHTML = '<p class="card-note"><span class="spinner"></span> 자료 범위 안에서 반론을 검토하고 있습니다.</p>';
      try {
        const res = await fetch("/api/red-team", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: briefing.question, outlook: briefing.outlook || {}, claims: briefing.claims || [], sources: (briefing.sources || []).map((s) => ({ source_id: s.source_id, title: s.title, url: s.url, published_at: s.published_at, excerpt: s.excerpt, provenance: s.provenance })) }) });
        const data = await res.json();
        const sources = (briefing.sources || []).filter((s) => (data.source_ids || []).includes(s.source_id));
        out.innerHTML = '<div class="redteam-output"><strong>' + (data.changed_conclusion ? '결론에 영향을 준 반론' : '반대편 검토') + '</strong><p>' + esc(data.review || data.note || "결과 없음") + '</p>' + (sources.map((s) => '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.title) + ' ↗</a>').join("") || '<small>응답에 연결된 출처 ID가 없습니다. 아래 주장·출처에서 반대 근거를 확인하세요.</small>') + '</div>';
      } catch (e) { out.innerHTML = '<p class="card-note">반대 심사 요청에 실패했습니다. 잠시 후 다시 시도해 주세요.</p>'; }
      redBtn.disabled = false; redBtn.textContent = "반대 심사 다시 실행";
    };
  }

  function openPresentation(briefing) {
    const sim = window.__SIM__ || {};
    const sources = briefing.sources || [];
    const claims = briefing.claims || [];
    const opposing = claims.filter((c) => c.status === "contested" || (c.contradicting_source_ids || []).length).slice(0, 3);
    const modal = document.createElement("div"); modal.className = "presentation-overlay"; modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true");
    modal.innerHTML = '<div class="presentation-card"><button class="presentation-close" type="button" aria-label="닫기">×</button><p class="overline">60 SECOND REVIEW</p><div class="presentation-pages"></div><div class="presentation-controls"><button class="btn-secondary" data-prev>이전</button><span></span><button class="btn-primary" data-next>다음</button></div></div>';
    const pages = [
      ["질문", briefing.question || "분석 질문"],
      ["핵심 근거", claims.slice(0, 3).map((c) => c.text).join("\n") || "검증된 핵심 주장이 없습니다."],
      ["반대 근거", opposing.map((c) => c.text).join("\n") || (briefing.outlook || {}).bear_evidence?.join("\n") || "확인된 반대 근거가 제한적입니다."],
      ["확률", "상승 " + pct((briefing.outlook || {}).probability_up) + " · 하락 " + pct((briefing.outlook || {}).probability_down) + " · 기대수익률 " + signed((briefing.outlook || {}).expected_return)],
      ["잔고 시나리오", (sim.scenarios || []).map((s) => s.label + ": " + signed(s.return_pct) + (sim.balance ? " → " + num(sim.balance * (1 + s.return_pct / 100)) + "원" : "")).join("\n") || "시뮬레이션 데이터가 없습니다."],
      ["한계와 출처", (briefing.uncertainties || []).slice(0, 2).map((u) => u.message).join("\n") + "\n출처 " + sources.length + "건 · 추정치는 투자 권유가 아닙니다."]
    ];
    let index = 0; let remaining = 60; let timer;
    const paint = () => { modal.querySelector(".presentation-pages").innerHTML = '<p class="overline">' + (index + 1) + ' / ' + pages.length + ' · 남은 시간 ' + remaining + '초</p><h2>' + esc(pages[index][0]) + '</h2><p class="presentation-copy">' + esc(pages[index][1]).replace(/\n/g, "<br>") + '</p>'; modal.querySelector("[data-prev]").disabled = index === 0; modal.querySelector("[data-next]").textContent = index === pages.length - 1 ? "닫기" : "다음"; };
    const close = () => { clearInterval(timer); modal.remove(); };
    modal.querySelector(".presentation-close").onclick = close;
    modal.querySelector("[data-prev]").onclick = () => { index = Math.max(0, index - 1); paint(); };
    modal.querySelector("[data-next]").onclick = () => { if (index === pages.length - 1) close(); else { index++; paint(); } };
    modal.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); if (e.key === "ArrowRight") modal.querySelector("[data-next]").click(); });
    document.body.appendChild(modal); paint(); modal.querySelector("[data-next]").focus();
    timer = setInterval(() => { remaining = Math.max(0, remaining - 1); paint(); if (!remaining) close(); }, 1000);
  }

  function renderFollowupsCard() {
    const prompts = [
      "반대 근거를 더 찾아서 정리해줘",
      "최근 공시를 중심으로 다시 살펴봐줘",
      "자료의 최신성과 출처를 더 자세히 확인해줘",
    ];
    return (
      '<section class="card followups-card"><p class="overline">NEXT QUESTIONS</p>' +
      '<h3 class="card-title">이어서 살펴보기</h3>' +
      '<div class="followup-actions">' +
      prompts.map((prompt) => '<button type="button" class="chip-btn" data-followup="' + esc(prompt) + '">' + esc(prompt) + '</button>').join("") +
      '</div></section>'
    );
  }

  function renderReliabilityCard(briefing) {
    const sources = briefing.sources || [];
    const officialProviders = new Set(["dart", "ecos", "kosis", "ir"]);
    const official = sources.filter((source) => source.provenance === "live" && officialProviders.has(source.provider)).length;
    const sample = sources.filter((source) => source.provenance === "sample" || source.provenance === "demo").length;
    const now = Date.now();
    let fresh = 0;
    let old = 0;
    let unknown = 0;
    sources.forEach((source) => {
      if (!source.published_at) {
        unknown++;
        return;
      }
      const published = Date.parse(source.published_at);
      if (!Number.isFinite(published)) unknown++;
      else if (now - published <= 365 * 24 * 60 * 60 * 1000) fresh++;
      else old++;
    });
    const conflicts = (briefing.uncertainties || []).filter((item) => item.kind === "conflict").length;
    const conflictMessages = (briefing.uncertainties || [])
      .filter((item) => item.kind === "conflict")
      .slice(0, 2)
      .map((item) => '<li>' + esc(item.message) + '</li>')
      .join("");
    return (
      '<section class="card reliability-card"><p class="overline">SOURCE CHECK</p>' +
      '<h3 class="card-title">자료 신뢰도 한눈에 보기</h3>' +
      '<p class="card-note">점수로 단정하지 않고, 확인 가능한 기준별로 나눠 표시합니다.</p>' +
      '<div class="reliability-grid">' +
      '<div><strong>' + official + '/' + sources.length + '</strong><span>공식 출처</span></div>' +
      '<div><strong>' + fresh + '</strong><span>1년 이내 자료</span></div>' +
      '<div><strong>' + old + '</strong><span>1년 지난 자료</span></div>' +
      '<div><strong>' + unknown + '</strong><span>발행일 미확인</span></div>' +
      '<div><strong>' + conflicts + '</strong><span>자료 상충</span></div>' +
      '<div><strong>' + sample + '</strong><span>샘플·데모 자료</span></div>' +
      '</div>' +
      (conflictMessages ? '<ul class="reliability-conflicts">' + conflictMessages + '</ul>' : '') +
      (sources.length ? '<a class="btn-ghost reliability-link" href="#sources-card">출처별 상세 확인 ↓</a>' : '<p class="card-note">수집된 출처가 없어 신뢰도 기준을 계산할 수 없습니다.</p>') +
      '</section>'
    );
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
      ? '<h4 class="scenario-heading">잔고 기준 12개월 결과 비교</h4><div class="balance-outcomes">' + scenarios.map((s) => {
          const estimated = Number(sim.balance) * (1 + Number(s.return_pct || 0) / 100);
          const delta = estimated - Number(sim.balance);
          return '<article class="balance-outcome" data-key="' + esc(s.key) + '" data-return="' + Number(s.return_pct || 0) + '"><span class="chip" style="--scenario-color:' + esc(s.color || "var(--primary)") + '">' + esc(s.label) + (s.key === "base" ? "" : " · " + pct(s.probability)) + '</span><strong class="outcome-value">' + num(estimated) + '원</strong><small class="outcome-delta ' + (delta >= 0 ? "is-up" : "is-down") + '">' + (delta >= 0 ? "+" : "") + num(delta) + '원 (' + signed(s.return_pct) + ')</small><span class="scenario-description">' + esc(s.description || "") + '</span></article>';
        }).join("") + "</div>" : "";
    const assumptionPanel = balanceOutcomes ? '<section class="assumption-panel"><h4 class="scenario-heading">가정 바꾸기 시뮬레이터</h4><p class="card-note">산업 민감도를 단순화한 교육용 참고치입니다. 실제 예측 모델이나 인과 추정이 아닙니다.</p>' + [
      ["금리 변화", "rate", -1, 1, 0.1, "%p"], ["원/달러 환율 변화", "fx", -10, 10, 1, "%"], ["반도체 가격 변화", "chip", -20, 20, 1, "%"]
    ].map((x) => '<label class="assumption-row"><span>' + x[0] + '</span><input type="range" min="' + x[2] + '" max="' + x[3] + '" step="' + x[4] + '" value="0" data-assumption="' + x[1] + '"><output data-value="' + x[1] + '">0' + x[5] + '</output></label>').join("") + '<p class="card-note" id="assumption-impact">가정을 조절하면 잔고별 민감도 참고치가 바뀝니다.</p></section>' : "";

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
      assumptionPanel +
      '<dl class="kv-list">' +
      '<div class="kv"><dt>데이터 출처</dt><dd>' +
      esc(sim.data_source || "—") +
      "</dd></div>" +
      "</dl>" +
      '<div class="chart-wrap" id="chart-wrap">' +
      '<svg class="chart" id="sim-chart" viewBox="0 0 900 340" role="img" aria-label="주가 시뮬레이션 차트"></svg>' +
      '<div class="chart-tooltip" id="chart-tooltip"></div>' +
      "</div>" +
      '<div class="chart-evidence-panel" id="chart-evidence-panel" aria-live="polite"><strong>전망 시점을 눌러 근거 확인</strong><span>점이나 선을 누르면 해당 시점의 AI 전망과 연결된 조사 자료를 보여드립니다.</span></div>' +
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

    const showPointDetails = (idx) => {
      const p = points[idx];
      const panel = $("chart-evidence-panel");
      if (!p || !panel) return;
      const briefing = window.__BRIEFING__ || {};
      const claims = briefing.claims || [];
      const sources = briefing.sources || [];
      const evidenceLines = p.evidence || [];
      const direction = Number(p.cumulative_return) >= 0 ? "상승" : "하락";
      const currency = (window.__SIM__ && window.__SIM__.currency) || "KRW";
      const matchedClaims = claims.filter((claim) => evidenceLines.some((line) => {
        const statement = String(line).split(" — ")[0];
        return statement.startsWith(String(claim.text || "").slice(0, 60)) || String(claim.text || "").startsWith(statement.slice(0, 60));
      }));
      const sourceIds = new Set(matchedClaims.flatMap((claim) => [
        ...(claim.supporting_source_ids || []), ...(claim.contradicting_source_ids || []),
      ]));
      const linkedSources = sources.filter((source) => sourceIds.has(source.source_id));
      panel.innerHTML = '<div class="chart-evidence-head"><strong>' + esc(p.date) + ' 전망 · ' + num(p.price) + ' ' + esc(currency) + '</strong><span>누적 ' + signed(p.cumulative_return) + ' · ' + direction + ' 확률 ' + pct(p.probability) + '</span></div>' +
        '<p>' + esc(p.detail || p.outlook || "근거 기반 시나리오") + '</p>' +
        (evidenceLines.length ? '<ul>' + evidenceLines.map((line) => '<li>' + esc(line) + '</li>').join("") + '</ul>' : '<span>이 시점에 연결할 근거 문장이 없습니다.</span>') +
        (linkedSources.length ? '<div class="chart-source-links"><strong>연결된 조사 자료</strong>' + linkedSources.slice(0, 6).map((source) => '<a href="' + esc(source.url) + '" target="_blank" rel="noopener noreferrer">' + esc(source.title) + ' · ' + esc(source.published_at || "발행일 미확인") + ' ↗</a>').join("") + '</div>' : '<span class="card-note">연결된 원문 출처는 아래 자료 출처 목록에서 확인해 주세요.</span>');
      panel.classList.add("is-active");
    };

    // hover와 클릭을 모두 지원해 모바일에서도 전망 근거를 열 수 있다.
    svg.querySelectorAll(".chart-hit").forEach((node) => {
      node.addEventListener("mouseenter", () => {
        const idx = Number(node.getAttribute("data-index"));
        const p = points[idx];
        if (!p) return;
        showPointDetails(idx);
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
      node.addEventListener("click", () => showPointDetails(Number(node.getAttribute("data-index"))));
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
    state.question = payload.question || state.question;
    state.step = "running";
    setBusy(true);
    el.composerInput.value = "";
    autoGrow();
    addMessage(
      "user",
      (payload.rerun
        ? "<strong>최신 자료로 다시 분석</strong><br />" + esc(payload.question)
        : payload.followup
        ? "<strong>이어서 질문</strong><br />" + esc(payload.followup)
        : payload.conversational
        ? "<strong>투자 잔고</strong><br />" + esc(payload.balance)
        : "<strong>직접 입력</strong><br />" + esc(payload.question)) +
        (payload.company ? "<br />기업: " + esc(payload.company) : "") +
        (payload.period ? "<br />기간: " + esc(payload.period) : "") +
        (payload.conversational ? "<br />질문: " + esc(payload.question) : "")
    );
    const thinking = addMessage(
      "assistant",
      '<span class="spinner"></span> <span class="muted">공개 자료를 확인하고 있습니다…</span>'
    );
    let timeoutId;
    let progressId;
    try {
      const previousBriefing = payload.rerun ? window.__BRIEFING__ : null;
      const balance = parseBalance(payload.balance);
      const body = {
        question: payload.question,
        company: payload.company,
        topic: payload.topic,
        period: payload.period,
        balance: balance,
      };
      const ir = (payload.ir_urls || "").trim();
      if (ir) body.ir_urls = [ir];
      const controller = new AbortController();
      // Vercel Hobby 요청이 장시간 매달려 사용자에게 4~5분 로딩으로 보이지 않게 제한한다.
      timeoutId = setTimeout(() => controller.abort(), 75000);
      const requestStarted = Date.now();
      progressId = setInterval(() => {
        const bubble = thinking.querySelector(".bubble");
        const seconds = Math.floor((Date.now() - requestStarted) / 1000);
        if (bubble && seconds >= 20) bubble.innerHTML = '<span class="spinner"></span> <span class="muted">공개 자료와 AI를 확인하고 있습니다. ' + seconds + '초째 진행 중…</span>';
      }, 5000);
      const res = await fetch("/api/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      clearInterval(progressId);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "요청 실패");
      thinking.remove();

      if (payload.rerun && previousBriefing) {
        const oldIds = new Set((previousBriefing.sources || []).map((s) => s.source_id));
        const added = (data.sources || []).filter((s) => !oldIds.has(s.source_id));
        const oldUp = Number((previousBriefing.outlook || {}).probability_up || 0);
        const newUp = Number((data.outlook || {}).probability_up || 0);
        data.changeSummary = { added: added.length, titles: added.slice(0, 5).map((s) => s.title), probabilityDelta: Math.round((newUp - oldUp) * 100) };
      }

      if (balance && !data.simulation) {
        const sim = await fetchSimulation(data, balance);
        if (sim) data.simulation = sim;
      }
      window.__SIM__ = data.simulation || null;
      state.step = "done";
      state.lastRequest = {
        question: payload.question,
        originalQuestion: payload.originalQuestion || payload.question,
        company: payload.company || "",
        topic: payload.topic || "",
        period: payload.period || "",
        balance: payload.balance || (balance ? num(balance) + "원" : ""),
        ir_urls: payload.ir_urls || "",
      };
      addMessage("assistant", payload.followup
        ? "추가 조사를 마쳤습니다. 갱신된 결과를 아래에서 확인해 주세요."
        : payload.rerun
        ? "최신 자료로 다시 분석했습니다. 이전 결과와 비교해 보세요."
        : "조사가 완료됐습니다. 아래에서 상승·하락 확률과 시뮬레이션을 확인해 주세요.");
      renderResult(data);
      loadRecent();
    } catch (err) {
      clearTimeout(timeoutId);
      clearInterval(progressId);
      thinking.remove();
      state.step = payload.balance ? "balance" : "welcome";
      const message = err.name === "AbortError"
        ? "조사가 75초 안에 끝나지 않아 연결을 종료했습니다. 질문과 잔고는 유지됩니다. 잠시 후 다시 시도해 주세요."
        : "조사 중 오류가 발생했습니다: " + (err.message || "알 수 없는 오류");
      addMessage("assistant", esc(message));
    } finally {
      clearTimeout(timeoutId);
      clearInterval(progressId);
      state.busy = false;
      setBusy(false);
      updateComposerPrompt(state.step);
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
    const mult = { "": 1, "만": 10000, "천만": 10000000, "억": 100000000, "조": 1000000000000 }[m[2] || ""];
    return value * mult;
  }

  function sendFollowup(prompt) {
    if (!state.lastRequest || state.busy) return;
    const request = state.lastRequest;
    const question = (request.originalQuestion || request.question) + "\n추가 요청: " + prompt;
    runDirect({ ...request, question, originalQuestion: request.originalQuestion || request.question, followup: prompt });
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
      const rerun = e.target.closest("#rerun-analysis");
      const followup = e.target.closest("[data-followup]");
      const present = e.target.closest("#presentation-mode");
      const watch = e.target.closest("#toggle-watch");
      if (copy) copyReport();
      if (dl) downloadReport();
      if (rerun && state.lastRequest && !state.busy) runDirect({ ...state.lastRequest, rerun: true });
      if (followup) sendFollowup(followup.getAttribute("data-followup"));
      if (present && window.__BRIEFING__) openPresentation(window.__BRIEFING__);
      if (watch && window.__BRIEFING__) toggleWatch(window.__BRIEFING__, watch);
    });

    // 방문할 때마다 새 대화를 환영 화면에서 시작한다. 진행 상태는
    // 브라우저 인스턴스 메모리에 의존하지 않도록 질문을 받은 뒤 로컬에서 이어간다.
    startNewChat();

    el.newChat.addEventListener("click", startNewChat);
    setInterval(checkWatchReminder, 60000);
  }

  async function toggleWatch(briefing, button) {
    const key = "sf-watch-run";
    if (localStorage.getItem(key) === String(briefing.run_id)) {
      localStorage.removeItem(key); localStorage.removeItem("sf-watch-at");
      button.textContent = "24시간 자료 확인 알림 켜기";
      const status = $("watch-status"); if (status) status.textContent = "알림이 꺼졌습니다.";
      return;
    }
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (e) { /* 알림 권한은 선택 사항 */ }
    }
    localStorage.setItem(key, String(briefing.run_id));
    localStorage.setItem("sf-watch-at", String(Date.now() + 24 * 60 * 60 * 1000));
    localStorage.setItem("sf-watch-request", JSON.stringify(state.lastRequest || { question: briefing.question }));
    button.textContent = "알림 끄기";
    const status = $("watch-status"); if (status) status.textContent = "24시간 뒤 최신 자료를 확인하라는 알림을 표시합니다. 브라우저를 열어 두어야 합니다.";
  }

  function checkWatchReminder() {
    const due = Number(localStorage.getItem("sf-watch-at") || 0);
    if (!due || Date.now() < due) return;
    localStorage.setItem("sf-watch-at", String(Date.now() + 24 * 60 * 60 * 1000));
    const request = JSON.parse(localStorage.getItem("sf-watch-request") || "{}");
    if ("Notification" in window && Notification.permission === "granted") {
      const note = new Notification("SignalForge 자료 확인", { body: "저장한 분석의 최신 자료를 다시 확인할 수 있습니다." });
      note.onclick = () => { window.focus(); if (request.question) runDirect({ ...request, rerun: true }); };
    }
    const status = $("watch-status");
    if (status) {
      status.textContent = "확인 시점입니다. 최신 자료 확인을 눌러 이전 결과와 비교하세요.";
      const link = document.createElement("button"); link.className = "btn-secondary btn-sm"; link.textContent = "최신 자료 확인"; link.onclick = () => runDirect({ ...request, rerun: true }); status.appendChild(link);
    }
  }

  async function startNewChat() {
    if (state.busy) return;
    el.messages.innerHTML = "";
    state.sessionId = null;
    state.step = "welcome";
    state.question = "";
    state.lastRequest = null;
    try { localStorage.removeItem("sf-session"); } catch (err) { /* 무시 */ }
    addMessage(
      "assistant",
      "환영합니다. <strong>무엇이 궁금하신가요?</strong><br />관심 있는 기업이나 산업을 입력하면 공개 자료를 찾아 근거와 반대 근거를 함께 정리해 드립니다."
    );
    renderQuickReplies(EXAMPLES);
    updateComposerPrompt(state.step);
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
