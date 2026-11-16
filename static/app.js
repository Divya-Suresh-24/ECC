/* ECC Protocol Assistant — front end (no build step, no external dependencies). */
(function () {
  "use strict";

  // ------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: { "X-Requested-With": "ecc" } };
    if (opts.json !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    } else if (opts.form) {
      init.body = opts.form;
    }
    const res = await fetch(path, init);
    if (res.status === 401) {
      window.location.href = "login";
      throw new Error("Please sign in.");
    }
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
    return data;
  }

  let toastTimer;
  function toast(msg, isError = false) {
    $$(".toast").forEach((t) => t.remove());
    const t = el("div", { class: "toast" + (isError ? " error" : ""), role: "status" }, msg);
    document.body.append(t);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.remove(), isError ? 6000 : 3000);
  }

  const fmtDate = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };
  const fmtDateTime = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const fmtSize = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");
  const typeBadge = (t) => el("span", { class: `badge ${t || ""}` }, (t || "file").toUpperCase());

  // ------------------------------------------------------------ minimal markdown with citation markers
  const CITE_OPEN = "⟦", CITE_CLOSE = "⟧";

  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[\s(])_([^_\s][^_]*?)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
    s = s.replace(new RegExp(`${CITE_OPEN}c:([0-9,]+)${CITE_CLOSE}`, "g"), (_, nums) =>
      nums.split(",").map((n) => `<button type="button" class="cite" data-n="${n}" aria-label="Source ${n}">${n}</button>`).join(""));
    return s;
  }

  function md(text) {
    const lines = text.replace(/\r/g, "").split("\n");
    const out = [];
    let list = null, para = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
    const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const raw of lines) {
      const line = raw.trimEnd();
      let m;
      if (!line.trim()) { flushPara(); closeList(); continue; }
      if ((m = line.match(/^\s{0,3}#{1,6}\s+(.*)$/))) { flushPara(); closeList(); out.push(`<h4>${inline(m[1])}</h4>`); continue; }
      if ((m = line.match(/^\s*(?:[-*•])\s+(.*)$/))) {
        flushPara(); if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
        out.push(`<li>${inline(m[1])}</li>`); continue;
      }
      if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
        flushPara(); if (list !== "ol") { closeList(); out.push(`<ol start="${m[1]}">`); list = "ol"; }
        out.push(`<li>${inline(m[2])}</li>`); continue;
      }
      if (list && /^\s{2,}\S/.test(raw) && out.length) {   // continuation of a list item
        out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, " " + inline(line.trim()) + "</li>");
        continue;
      }
      closeList(); para.push(line);
    }
    flushPara(); closeList();
    return out.join("");
  }

  function answerMarkdown(segments) {
    let s = "";
    for (const seg of segments || []) {
      let t = seg.text || "";
      if (seg.cites && seg.cites.length) {
        const m = t.match(/\s*$/);
        const trail = m ? m[0] : "";
        t = t.slice(0, t.length - trail.length) + `${CITE_OPEN}c:${seg.cites.join(",")}${CITE_CLOSE}` + trail;
      }
      s += t;
    }
    return s;
  }

  function highlight(text, cited) {
    // Return escaped HTML of `text` with each cited snippet wrapped in <mark>.
    const ranges = [];
    for (const c of cited || []) {
      const needle = (c || "").trim();
      if (!needle) continue;
      let i = text.indexOf(needle);
      if (i < 0) i = text.indexOf(needle.slice(0, 80));
      if (i >= 0) ranges.push([i, i + (text.indexOf(needle) >= 0 ? needle.length : Math.min(needle.length, text.length - i))]);
    }
    ranges.sort((a, b) => a[0] - b[0]);
    let html = "", pos = 0;
    for (const [s, e] of ranges) {
      if (s < pos) continue;
      html += esc(text.slice(pos, s)) + "<mark>" + esc(text.slice(s, e)) + "</mark>";
      pos = e;
    }
    return html + esc(text.slice(pos));
  }

  // ------------------------------------------------------------ state
  const S = {
    me: null, app: null,
    conv: { id: null, turns: [] },
    scope: null,            // {id, title} when asking about one document
    busy: false,
    activeCite: null,
    lib: { q: "", cat: "" },
    facSub: "docs",
  };

  // ------------------------------------------------------------ boot
  async function boot() {
    const me = await api("api/me");
    S.me = me.user; S.app = me.app;
    $("#uname").textContent = S.me.display_name;
    $("#avatar").textContent = initials(S.me.display_name);
    $("#rolepill").textContent = S.me.role === "faculty" ? "Faculty" : "Resident/Intern";
    $("#rolepill").classList.toggle("faculty", S.me.role === "faculty");
    $("#brand-sub").textContent = S.app.institution;
    if (S.me.role === "faculty") $("#faculty-tab").hidden = false;

    $$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
    $("#userbtn").addEventListener("click", (e) => {
      e.stopPropagation();
      const d = $("#userdrop"); d.hidden = !d.hidden;
      $("#userbtn").setAttribute("aria-expanded", String(!d.hidden));
    });
    document.addEventListener("click", () => { $("#userdrop").hidden = true; });
    $("#userdrop").addEventListener("click", async (e) => {
      const a = e.target.closest("button")?.dataset.action;
      if (a === "logout") { await api("api/logout", { method: "POST" }); window.location.href = "login"; }
      if (a === "password") passwordModal(false);
    });

    setupAsk();
    const hash = location.hash.replace("#", "");
    showView(["library", "faculty"].includes(hash) && (hash !== "faculty" || S.me.role === "faculty") ? hash : "ask");
    if (S.me.must_change_pw) passwordModal(true);
  }

  const initials = (n) => (n || "?").split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();

  function showView(name) {
    $$(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.view === name)));
    $$(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
    history.replaceState(null, "", name === "ask" ? location.pathname : `#${name}`);
    if (name === "library") loadLibrary();
    if (name === "faculty") showFacSub(S.facSub);
    if (name === "ask") $("#question").focus();
  }

  // ------------------------------------------------------------ ASK
  const SUGGESTIONS = [
    ["Triage", "What triage status does a dyspneic patient get, and how fast must they be seen?"],
    ["After hours", "What's the process for an after-hours surgery case?"],
    ["Neuro", "When is a thoracolumbar IVDD dog a surgical emergency?"],
    ["Rabies", "A dog that bit a person was just admitted. What does the rabies SOP require?"],
    ["Imaging", "How do I send radiographs to VitalRads?"],
    ["Policy", "How does the Good Samaritan policy work for a stray brought in?"],
  ];

  function setupAsk() {
    const ta = $("#question");
    ta.addEventListener("input", autosize);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
    });
    $("#composer").addEventListener("submit", (e) => { e.preventDefault(); ask(ta.value); });
    $("#new-chat").addEventListener("click", newChat);
    $("#new-chat-m").addEventListener("click", newChat);
    $("#thread").addEventListener("click", onThreadClick);
    renderThread();
    loadHistory();
  }

  function autosize() {
    const ta = $("#question");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
  }

  function newChat() {
    S.conv = { id: null, turns: [] };
    closePanel();
    renderThread();
    $$(".history-item").forEach((h) => h.classList.remove("active"));
    showView("ask");
  }

  function setScope(doc) {
    S.scope = doc;
    const box = $("#scope");
    box.innerHTML = "";
    if (!doc) { box.hidden = true; return; }
    box.hidden = false;
    box.append(
      el("span", { class: "badge" }, "Only searching"),
      el("strong", { class: "small" }, doc.title),
      el("button", { class: "btn ghost small", type: "button", onclick: () => setScope(null) }, "✕ Search all documents"),
    );
  }

  async function loadHistory() {
    const list = $("#history-list");
    try {
      const convs = await api("api/conversations");
      list.innerHTML = "";
      if (!convs.length) list.append(el("div", { class: "muted small", style: "padding:6px 10px" }, "Your questions will appear here."));
      for (const c of convs) {
        list.append(el("div", { class: "history-item" + (c.id === S.conv.id ? " active" : ""), "data-id": c.id },
          el("button", { class: "open", title: c.title, onclick: () => openConversation(c.id) }, c.title || "Untitled"),
          el("button", { class: "del", title: "Delete", "aria-label": "Delete conversation", onclick: async (e) => {
            e.stopPropagation();
            if (!confirm("Delete this conversation?")) return;
            await api(`api/conversations/${c.id}`, { method: "DELETE" });
            if (S.conv.id === c.id) newChat();
            loadHistory();
          } }, "✕")));
      }
    } catch (e) { /* non-fatal */ }
  }

  async function openConversation(id) {
    try {
      const c = await api(`api/conversations/${id}`);
      S.conv = { id: c.id, turns: c.turns };
      closePanel();
      renderThread();
      $$(".history-item").forEach((h) => h.classList.toggle("active", Number(h.dataset.id) === id));
    } catch (e) { toast(e.message, true); }
  }

  function renderThread() {
    const inner = $("#thread-inner");
    inner.innerHTML = "";
    if (!S.conv.turns.length) {
      const welcome = el("div", { class: "welcome" },
        el("h1", {}, `Hi ${S.me.display_name.split(" ")[0]} — what do you need to look up?`),
        el("p", {}, "Ask in plain language. Every answer is drawn from ISU ECC protocols, SOPs and teaching files, with numbered citations you can open to check the source."),
        S.app.mock ? el("div", { class: "callout" }, "Test mode: no AI model is connected, so answers show the matching passages instead of a written answer.") : null,
        el("div", { class: "suggestions" }, SUGGESTIONS.map(([tag, q]) =>
          el("button", { class: "suggestion", type: "button", onclick: () => ask(q) }, el("b", {}, tag), q))));
      inner.append(welcome);
      return;
    }
    S.conv.turns.forEach((t, i) => inner.append(renderTurn(t, i)));
    requestAnimationFrame(() => { const th = $("#thread"); th.scrollTop = th.scrollHeight; });
  }

  function renderTurn(t, idx) {
    const wrap = el("div", { class: "turn", "data-idx": idx });
    wrap.append(el("div", { class: "q" }, el("div", { class: "q-bubble" }, t.question)));
    if (t.pending) {
      wrap.append(el("div", { class: "a" }, el("span", { class: "thinking" },
        el("span", { class: "dots" }, el("span"), el("span"), el("span")), "Searching the ECC documents…")));
      return wrap;
    }
    if (t.error) {
      wrap.append(el("div", { class: "a notfound" }, el("div", { class: "a-body" }, el("p", {}, t.error)),
        el("div", { class: "a-actions" }, el("button", { class: "btn small", onclick: () => { S.conv.turns.splice(idx, 1); ask(t.question); } }, "Try again"))));
      return wrap;
    }
    const a = el("div", { class: "a" + (t.found ? "" : " notfound") });
    a.append(el("div", { class: "a-body", html: md(answerMarkdown(t.segments)) }));
    if (t.sources && t.sources.length) {
      a.append(el("div", { class: "sources" },
        el("div", { class: "sources-label" }, "Sources"),
        el("div", { class: "source-chips" }, t.sources.map((s) =>
          el("button", { class: "source-chip", type: "button", "data-n": s.n, title: `${s.title} — ${s.locator}` },
            el("span", { class: "n" }, s.n), el("span", { class: "t" }, s.title), el("span", { class: "loc" }, s.locator))))));
    }
    const up = el("button", { class: "btn ghost" + (t.feedback === 1 ? " on" : ""), title: "Helpful", "aria-label": "Helpful" }, "👍 Helpful");
    const down = el("button", { class: "btn ghost" + (t.feedback === -1 ? " on" : ""), title: "Not helpful / wrong", "aria-label": "Not helpful" }, "👎 Not right");
    up.addEventListener("click", () => sendFeedback(t, 1));
    down.addEventListener("click", () => sendFeedback(t, -1));
    const copy = el("button", { class: "btn ghost", title: "Copy answer" }, "Copy");
    copy.addEventListener("click", () => {
      const text = (t.segments || []).map((s) => s.text + (s.cites?.length ? ` [${s.cites.join(",")}]` : "")).join("") +
        "\n\nSources:\n" + (t.sources || []).map((s) => `[${s.n}] ${s.title}, ${s.locator}`).join("\n");
      navigator.clipboard?.writeText(text).then(() => toast("Copied with sources"));
    });
    a.append(el("div", { class: "a-actions" }, up, down, el("span", { class: "spacer" }), copy));
    wrap.append(a);
    return wrap;
  }

  async function sendFeedback(t, value) {
    if (!t.question_id) return;
    let note = "";
    const newVal = t.feedback === value ? 0 : value;
    if (newVal === -1) {
      note = prompt("What was wrong or missing? (optional — this goes to ECC faculty)") || "";
    }
    try {
      await api(`api/questions/${t.question_id}/feedback`, { method: "POST", json: { value: newVal, note } });
      t.feedback = newVal || null;
      renderThread();
      if (newVal === -1) toast("Thanks — faculty will review this answer.");
    } catch (e) { toast(e.message, true); }
  }

  async function ask(text) {
    const question = (text || "").trim();
    if (!question || S.busy) return;
    if (question.length > (S.app.max_question_chars || 1500)) { toast("Please shorten your question.", true); return; }
    S.busy = true;
    $("#send").disabled = true;
    $("#question").value = "";
    autosize();
    const turn = { question, pending: true };
    S.conv.turns.push(turn);
    renderThread();
    try {
      const res = await api("api/ask", { method: "POST", json: {
        question, conversation_id: S.conv.id, doc_ids: S.scope ? [S.scope.id] : null } });
      Object.assign(turn, res, { pending: false });
      const isNew = !S.conv.id;
      S.conv.id = res.conversation_id;
      if (isNew) loadHistory();
    } catch (e) {
      Object.assign(turn, { pending: false, error: e.message });
    } finally {
      S.busy = false;
      $("#send").disabled = false;
      renderThread();
      $("#question").focus();
    }
  }

  function onThreadClick(e) {
    const btn = e.target.closest(".cite, .source-chip");
    if (!btn) return;
    const idx = Number(btn.closest(".turn").dataset.idx);
    const turn = S.conv.turns[idx];
    const src = (turn.sources || []).find((s) => s.n === Number(btn.dataset.n));
    if (src) openSource(src, idx);
  }

  // ------------------------------------------------------------ source panel
  function closePanel() {
    $("#panel").hidden = true;
    $("#ask-grid").classList.remove("with-panel");
    $$(".cite.active, .source-chip.active").forEach((b) => b.classList.remove("active"));
  }

  async function openSource(src, turnIdx) {
    $$(".cite.active, .source-chip.active").forEach((b) => b.classList.remove("active"));
    $$(`.turn[data-idx="${turnIdx}"] [data-n="${src.n}"]`).forEach((b) => b.classList.add("active"));
    const panel = $("#panel");
    panel.hidden = false;
    $("#ask-grid").classList.add("with-panel");
    panel.innerHTML = "";
    const body = el("div", { class: "panel-body" }, el("div", { class: "muted small" }, "Loading passage…"));
    panel.append(
      el("div", { class: "panel-head" },
        el("div", { style: "flex:1;min-width:0" },
          el("div", { class: "small muted", style: "font-weight:700" }, `SOURCE ${src.n}`),
          el("h3", {}, src.title),
          el("div", { class: "meta-row" }, typeBadge(src.file_type), el("span", { class: "badge" }, src.locator),
            el("span", { class: "badge" }, src.category),
            src.effective_date ? el("span", { class: "badge" }, `Effective ${src.effective_date}`) : null)),
        el("button", { class: "close", "aria-label": "Close", onclick: closePanel }, "×")),
      body);
    let chunk;
    try {
      chunk = await api(`api/chunks/${src.chunk_id}`);
    } catch (e) {
      body.innerHTML = "";
      body.append(el("div", { class: "callout" }, e.message));
      return;
    }
    body.innerHTML = "";
    if (src.cited_texts && src.cited_texts.length) {
      body.append(el("div", { class: "sources-label" }, "Cited text"));
      src.cited_texts.forEach((c) => body.append(el("blockquote", { class: "quote" }, c)));
    }
    body.append(el("div", { class: "sources-label" }, `Full passage · ${chunk.locator}`));
    body.append(el("div", { class: "passage", html: highlight(chunk.text, src.cited_texts) }));
    body.append(el("p", { class: "muted small", style: "margin-top:14px" }, `Document last updated ${fmtDate(chunk.updated_at)}.`));

    const foot = el("div", { class: "panel-foot" },
      el("button", { class: "btn", onclick: () => openViewer(chunk.doc_id, chunk.id) }, "Open in document"));
    if (chunk.downloadable && chunk.file_type === "pdf") {
      foot.append(el("a", { class: "btn", href: `files/${chunk.doc_id}?inline=1#page=${chunk.page || 1}`, target: "_blank", rel: "noopener" }, "View PDF page ↗"));
    }
    if (chunk.downloadable) foot.append(el("a", { class: "btn", href: `files/${chunk.doc_id}` }, "Download"));
    else foot.append(el("span", { class: "muted small" }, "View-only document"));
    panel.append(foot);
  }

  // ------------------------------------------------------------ document viewer (modal)
  function modal({ title, sub, body, foot, narrow = false, locked = false }) {
    const root = $("#modal-root");
    root.innerHTML = "";
    const close = () => { if (!locked) { root.innerHTML = ""; document.removeEventListener("keydown", onKey); } };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    const m = el("div", { class: "modal" + (narrow ? " narrow" : ""), role: "dialog", "aria-modal": "true" },
      el("div", { class: "modal-head" },
        el("div", { style: "flex:1;min-width:0" }, el("h3", {}, title), sub || null),
        locked ? null : el("button", { class: "close", "aria-label": "Close", onclick: close }, "×")),
      el("div", { class: "modal-body" }, body),
      foot ? el("div", { class: "modal-foot" }, foot) : null);
    const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } }, m);
    root.append(back);
    return { close: () => { locked = false; close(); }, node: m };
  }

  async function openViewer(docId, targetChunkId) {
    let d;
    try { d = await api(`api/documents/${docId}/text`); } catch (e) { toast(e.message, true); return; }
    const body = el("div", {},
      d.description ? el("p", { class: "muted" }, d.description) : null,
      d.passages.length ? null : el("div", { class: "empty" }, "No text could be extracted from this file."),
      d.passages.map((p) => el("div", { class: "viewer-passage" + (p.id === targetChunkId ? " target" : ""), id: `vp-${p.id}` },
        el("div", { class: "loc" }, p.locator), el("div", { class: "txt" }, p.text))));
    const foot = [];
    foot.push(el("button", { class: "btn", onclick: () => { m.close(); showView("ask"); setScope({ id: d.id, title: d.title }); newChatKeepScope(); } }, "Ask about this document"));
    if (d.downloadable && d.file_type === "pdf") foot.push(el("a", { class: "btn", href: `files/${d.id}?inline=1`, target: "_blank", rel: "noopener" }, "Open original PDF ↗"));
    if (d.downloadable) foot.push(el("a", { class: "btn primary", href: `files/${d.id}` }, `Download ${d.file_type?.toUpperCase() || ""}`));
    else foot.push(el("span", { class: "muted small", style: "align-self:center" }, "Faculty have made this document view-only."));
    const m = modal({
      title: d.title,
      sub: el("div", { class: "meta-row" }, typeBadge(d.file_type), el("span", { class: "badge" }, d.category),
        d.effective_date ? el("span", { class: "badge" }, `Effective ${d.effective_date}`) : null,
        el("span", { class: "muted small" }, `Updated ${fmtDate(d.updated_at)} · v${d.version}`),
        d.status === "retired" ? el("span", { class: "badge bad" }, "Retired") : null),
      body, foot,
    });
    if (targetChunkId) setTimeout(() => document.getElementById(`vp-${targetChunkId}`)?.scrollIntoView({ block: "start" }), 30);
  }

  function newChatKeepScope() {
    S.conv = { id: null, turns: [] };
    closePanel(); renderThread();
    $$(".history-item").forEach((h) => h.classList.remove("active"));
    $("#question").focus();
  }

  // ------------------------------------------------------------ LIBRARY
  let libTimer;
  async function loadLibrary() {
    const cats = $("#lib-cats");
    if (!cats.childElementCount) {
      const mk = (label, val) => el("button", { class: "chip", "aria-pressed": String(S.lib.cat === val), onclick: () => { S.lib.cat = val; $$(".chip", cats).forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.val === val))); fetchLibrary(); }, "data-val": val }, label);
      cats.append(mk("All", ""), ...S.app.categories.map((c) => mk(c, c)));
      $("#lib-search").addEventListener("input", (e) => { clearTimeout(libTimer); libTimer = setTimeout(() => { S.lib.q = e.target.value; fetchLibrary(); }, 250); });
    }
    fetchLibrary();
  }

  async function fetchLibrary() {
    const list = $("#lib-list");
    const params = new URLSearchParams({ q: S.lib.q, category: S.lib.cat });
    let docs;
    try { docs = await api(`api/documents?${params}`); } catch (e) { toast(e.message, true); return; }
    list.innerHTML = "";
    if (!docs.length) { list.append(el("div", { class: "empty" }, S.lib.q ? "No documents match that search." : "No documents yet.")); return; }
    let lastCat = null;
    for (const d of docs) {
      if (!S.lib.q && d.category !== lastCat) { list.append(el("div", { class: "group-label" }, d.category)); lastCat = d.category; }
      list.append(el("div", { class: "card doc" },
        el("div", { style: "min-width:0" },
          el("div", { class: "doc-title" }, el("button", { onclick: () => openViewer(d.id, d.match?.chunk_id) }, d.title)),
          el("div", { class: "doc-meta" }, typeBadge(d.file_type), S.lib.q ? el("span", { class: "badge" }, d.category) : null,
            d.effective_date ? el("span", {}, `Effective ${d.effective_date}`) : null,
            el("span", {}, `Updated ${fmtDate(d.updated_at)}`),
            d.page_count ? el("span", {}, `${d.page_count} ${d.file_type === "pptx" ? "slides" : "pages"}`) : null,
            d.downloadable ? null : el("span", { class: "badge warn" }, "View only")),
          d.match ? el("div", { class: "doc-match" }, el("strong", {}, d.match.locator + ": "), d.match.text + "…") : null),
        el("div", { class: "doc-actions" },
          el("button", { class: "btn small", onclick: () => openViewer(d.id, d.match?.chunk_id) }, "View"),
          el("button", { class: "btn small", onclick: () => { showView("ask"); setScope({ id: d.id, title: d.title }); newChatKeepScope(); } }, "Ask"),
          d.downloadable ? el("a", { class: "btn small", href: `files/${d.id}` }, "Download") : null)));
    }
  }

  // ------------------------------------------------------------ password modal
  function passwordModal(forced) {
    const cur = el("input", { class: "input", type: "password", autocomplete: "current-password" });
    const nw = el("input", { class: "input", type: "password", autocomplete: "new-password" });
    const nw2 = el("input", { class: "input", type: "password", autocomplete: "new-password" });
    const err = el("div", { class: "small", style: "color:var(--bad);min-height:18px" });
    const save = el("button", { class: "btn primary" }, "Save password");
    const m = modal({
      title: forced ? "Set your own password" : "Change password",
      narrow: true, locked: forced,
      body: el("div", {},
        forced ? el("div", { class: "callout" }, "You signed in with a temporary password. Choose a new one to continue.") : null,
        el("label", { class: "field" }, forced ? "Temporary password" : "Current password", cur),
        el("label", { class: "field" }, "New password", nw),
        el("label", { class: "field" }, "Repeat new password", nw2),
        el("div", { class: "muted small" }, "At least 10 characters with letters and numbers, or a 16+ character passphrase."),
        err),
      foot: [save],
    });
    save.addEventListener("click", async () => {
      err.textContent = "";
      if (nw.value !== nw2.value) { err.textContent = "New passwords don't match."; return; }
      try {
        await api("api/me/password", { method: "POST", json: { current: cur.value, new: nw.value } });
        S.me.must_change_pw = 0;
        m.close();
        toast("Password updated");
      } catch (e) { err.textContent = e.message; }
    });
    cur.focus();
  }

  // ------------------------------------------------------------ FACULTY
  function showFacSub(name) {
    S.facSub = name;
    $$(".subtab").forEach((t) => {
      t.setAttribute("aria-selected", String(t.dataset.sub === name));
      t.onclick = () => showFacSub(t.dataset.sub);
    });
    ["docs", "questions", "users", "activity"].forEach((n) => ($(`#fac-${n}`).hidden = n !== name));
    ({ docs: facDocs, questions: facQuestions, users: facUsers, activity: facActivity })[name]();
  }

  // ---- documents
  const facDocState = { q: "", status: "active" };

  async function facDocs() {
    const root = $("#fac-docs");
    root.innerHTML = "";
    const stats = el("div", { class: "stats" });
    root.append(stats);
    api("api/admin/stats").then((s) => {
      const stat = (num, lbl, onclick) => el("div", { class: "card stat", style: onclick ? "cursor:pointer" : "", onclick }, el("div", { class: "num" }, num), el("div", { class: "lbl" }, lbl));
      stats.append(
        stat(s.documents, `active documents${s.retired ? ` (+${s.retired} retired)` : ""}`),
        stat(s.questions_week, "questions in the last 7 days", () => showFacSub("questions")),
        stat(s.not_found_week, "unanswered this week (possible gaps)", () => { facQState.filter = "notfound"; showFacSub("questions"); }),
        stat(s.negative_unreviewed, "👎 answers waiting for review", () => { facQState.filter = "negative"; showFacSub("questions"); }),
        stat(s.users, "active accounts", () => showFacSub("users")));
    }).catch(() => {});

    // upload
    const fileInput = el("input", { type: "file", multiple: true, accept: ".pdf,.docx,.pptx,.txt,.md", hidden: true });
    const catSel = el("select", { class: "input" }, el("option", { value: "" }, "Auto-detect from filename"), S.app.categories.map((c) => el("option", { value: c }, c)));
    const dl = el("input", { type: "checkbox", checked: true });
    const eff = el("input", { class: "input", type: "date" });
    const results = el("div", { class: "results" });
    const drop = el("div", { class: "drop", tabindex: "0", role: "button" },
      el("div", {}, el("strong", {}, "Drop files here"), " or click to choose"),
      el("div", { class: "muted small" }, `PDF, Word (.docx), PowerPoint (.pptx), .txt or .md · up to ${S.app.max_upload_mb} MB each · select many at once`));
    drop.addEventListener("click", () => fileInput.click());
    drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") fileInput.click(); });
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));
    fileInput.addEventListener("change", () => upload(fileInput.files));

    async function upload(files) {
      if (!files || !files.length) return;
      const list = Array.from(files);
      results.innerHTML = "";
      results.append(el("div", { class: "muted" }, `Uploading and indexing ${list.length} file(s)…`));
      // send in small batches so big uploads don't time out
      const all = [];
      for (let i = 0; i < list.length; i += 5) {
        const fd = new FormData();
        list.slice(i, i + 5).forEach((f) => fd.append("files", f));
        fd.append("category", catSel.value);
        fd.append("downloadable", dl.checked ? "true" : "false");
        fd.append("effective_date", eff.value);
        try {
          const r = await api("api/admin/documents", { method: "POST", form: fd });
          all.push(...r.results);
        } catch (e) { all.push(...list.slice(i, i + 5).map((f) => ({ filename: f.name, ok: false, error: e.message }))); }
      }
      results.innerHTML = "";
      for (const r of all) {
        results.append(el("div", {}, r.ok ? el("span", { class: "badge ok" }, "Added") : el("span", { class: "badge bad" }, "Skipped"), " ",
          el("strong", {}, r.filename), " ", r.ok ? el("span", { class: "muted" }, `→ “${r.title}”, ${r.passages} passages`) : el("span", { class: "muted" }, r.error),
          (r.warnings || []).map((w) => el("div", { class: "small", style: "color:var(--warn);margin-left:58px" }, "⚠ " + w))));
      }
      fileInput.value = "";
      loadDocTable();
    }

    root.append(el("div", { class: "card", style: "padding:16px;margin-bottom:18px" },
      el("div", { style: "font-weight:700;margin-bottom:10px" }, "Add documents"),
      drop, fileInput,
      el("div", { class: "upload-opts" },
        el("label", { class: "field", style: "min-width:240px" }, "Category", catSel),
        el("label", { class: "field" }, "Effective date (optional)", eff),
        el("label", { class: "check", style: "margin-bottom:8px" }, dl, "Residents/interns can download the file")),
      el("div", { class: "muted small", style: "margin-top:8px" }, "To update a protocol, use ", el("strong", {}, "Replace file"), " on its row below — the old version is archived and the assistant immediately switches to the new one."),
      results));

    // table
    const search = el("input", { class: "input", type: "search", placeholder: "Filter by title…", value: facDocState.q, style: "max-width:300px" });
    const status = el("select", { class: "input", style: "max-width:180px" },
      ["active", "retired", "all"].map((s) => el("option", { value: s, selected: facDocState.status === s }, { active: "Active", retired: "Retired", all: "All" }[s])));
    search.addEventListener("input", () => { facDocState.q = search.value; renderDocRows(); });
    status.addEventListener("change", () => { facDocState.status = status.value; renderDocRows(); });
    const reindex = el("button", { class: "btn small ghost", title: "Re-extract text from every file" }, "Re-index all");
    reindex.addEventListener("click", async () => {
      if (!confirm("Re-extract and re-index every document? This can take a minute.")) return;
      reindex.disabled = true;
      try { const r = await api("api/admin/reindex", { method: "POST" }); toast(`Re-indexed ${r.passages} passages`); } catch (e) { toast(e.message, true); }
      reindex.disabled = false;
    });
    const tbody = el("tbody");
    root.append(el("div", { class: "toolbar" }, search, status, el("span", { class: "spacer" }), reindex),
      el("div", { class: "card table-wrap" }, el("table", { class: "data" },
        el("thead", {}, el("tr", {}, ["Title", "Category", "Type", "Version", "Updated", "Download", "Status", ""].map((h) => el("th", {}, h)))),
        tbody)));

    let docs = [];
    async function loadDocTable() {
      try { docs = await api("api/documents?all=1"); } catch (e) { toast(e.message, true); return; }
      renderDocRows();
    }
    function renderDocRows() {
      tbody.innerHTML = "";
      const q = facDocState.q.toLowerCase();
      const rows = docs.filter((d) => (facDocState.status === "all" || d.status === facDocState.status) && (!q || d.title.toLowerCase().includes(q)));
      if (!rows.length) { tbody.append(el("tr", {}, el("td", { colspan: 8, class: "empty" }, "No documents."))); return; }
      for (const d of rows) {
        const sw = el("input", { type: "checkbox", checked: !!d.downloadable, "aria-label": "Downloadable" });
        sw.addEventListener("change", async () => {
          try { await api(`api/admin/documents/${d.id}`, { method: "PATCH", json: { downloadable: sw.checked } }); d.downloadable = sw.checked ? 1 : 0; toast(sw.checked ? "Download enabled" : "Now view-only"); }
          catch (e) { sw.checked = !sw.checked; toast(e.message, true); }
        });
        tbody.append(el("tr", { class: d.status === "retired" ? "retired" : "" },
          el("td", { style: "min-width:240px" }, el("a", { href: "#", onclick: (e) => { e.preventDefault(); openViewer(d.id); } }, d.title),
            d.passages === 0 ? el("div", { class: "small", style: "color:var(--warn)" }, "⚠ no searchable text") : null),
          el("td", { class: "small" }, d.category),
          el("td", {}, typeBadge(d.file_type)),
          el("td", { class: "small" }, `v${d.current_version}`),
          el("td", { class: "small", title: d.updated_by ? `by ${d.updated_by}` : "" }, fmtDate(d.updated_at)),
          el("td", {}, el("label", { class: "switch" }, sw, el("span"))),
          el("td", {}, d.status === "active" ? el("span", { class: "badge ok" }, "Active") : el("span", { class: "badge" }, "Retired")),
          el("td", { style: "white-space:nowrap" },
            el("button", { class: "btn small", onclick: () => editDoc(d.id, loadDocTable) }, "Edit"), " ",
            el("button", { class: "btn small", onclick: () => replaceFile(d, loadDocTable) }, "Replace file"))));
      }
    }
    loadDocTable();
  }

  function replaceFile(d, done) {
    const input = el("input", { type: "file", accept: ".pdf,.docx,.pptx,.txt,.md" });
    input.addEventListener("change", async () => {
      if (!input.files[0]) return;
      const note = prompt(`Replacing “${d.title}” with ${input.files[0].name}.\nOptional note about what changed:`, "");
      if (note === null) return;
      const fd = new FormData();
      fd.append("file", input.files[0]);
      fd.append("note", note);
      try {
        const r = await api(`api/admin/documents/${d.id}/file`, { method: "POST", form: fd });
        toast(`Updated to v${r.version} (${r.passages} passages)`);
        if (r.warnings?.length) toast("⚠ " + r.warnings.join(" "), true);
        done();
      } catch (e) { toast(e.message, true); }
    });
    input.click();
  }

  async function editDoc(id, done) {
    let d;
    try { d = await api(`api/admin/documents/${id}`); } catch (e) { toast(e.message, true); return; }
    const f = {
      title: el("input", { class: "input", value: d.title }),
      category: el("select", { class: "input" }, S.app.categories.map((c) => el("option", { value: c, selected: c === d.category }, c))),
      effective_date: el("input", { class: "input", value: d.effective_date || "", placeholder: "e.g. 2026-06-01" }),
      tags: el("input", { class: "input", value: d.tags || "", placeholder: "extra search words, e.g. blocked cat, UO" }),
      description: el("textarea", { class: "input", rows: 2 }, d.description || ""),
      downloadable: el("input", { type: "checkbox", checked: !!d.downloadable }),
    };
    const versions = el("div", {}, el("div", { class: "sources-label", style: "margin-top:8px" }, "Version history"),
      el("table", { class: "data" }, el("tbody", {}, d.versions.map((v) => el("tr", {},
        el("td", {}, `v${v.version}`), el("td", { class: "small" }, v.filename), el("td", { class: "small" }, fmtSize(v.size_bytes)),
        el("td", { class: "small" }, `${fmtDate(v.uploaded_at)}${v.uploaded_by ? " · " + v.uploaded_by : ""}`),
        el("td", { class: "small muted" }, v.note || ""),
        el("td", {}, el("a", { class: "btn small", href: `files/${d.id}/v/${v.version}` }, "Download")))))));
    const save = el("button", { class: "btn primary" }, "Save changes");
    const retire = el("button", { class: "btn" }, d.status === "active" ? "Retire (hide from assistant)" : "Restore");
    const del = el("button", { class: "btn danger" }, "Delete permanently");
    const m = modal({
      title: "Edit document",
      sub: el("div", { class: "muted small" }, `${d.passages} searchable passages · cited in ${d.times_cited} answers`),
      body: el("div", {},
        el("label", { class: "field" }, "Title", f.title),
        el("div", { class: "grid2" }, el("label", { class: "field" }, "Category", f.category), el("label", { class: "field" }, "Effective / revision date", f.effective_date)),
        el("label", { class: "field" }, "Search keywords", f.tags),
        el("label", { class: "field" }, "Description (shown in the library)", f.description),
        el("label", { class: "check" }, f.downloadable, "Residents/interns can download the original file"),
        versions),
      foot: [del, retire, el("span", { class: "spacer", style: "flex:1" }), save],
    });
    save.addEventListener("click", async () => {
      try {
        await api(`api/admin/documents/${id}`, { method: "PATCH", json: {
          title: f.title.value, category: f.category.value, effective_date: f.effective_date.value,
          tags: f.tags.value, description: f.description.value, downloadable: f.downloadable.checked } });
        m.close(); toast("Saved"); done();
      } catch (e) { toast(e.message, true); }
    });
    retire.addEventListener("click", async () => {
      const to = d.status === "active" ? "retired" : "active";
      try { await api(`api/admin/documents/${id}`, { method: "PATCH", json: { status: to } }); m.close(); toast(to === "retired" ? "Retired — no longer used in answers" : "Restored"); done(); }
      catch (e) { toast(e.message, true); }
    });
    del.addEventListener("click", async () => {
      if (!confirm(`Permanently delete “${d.title}” and all its versions? Retiring is usually better (keeps history).`)) return;
      try { await api(`api/admin/documents/${id}`, { method: "DELETE" }); m.close(); toast("Deleted"); done(); }
      catch (e) { toast(e.message, true); }
    });
  }

  // ---- questions
  const facQState = { filter: "unreviewed", q: "" };

  async function facQuestions() {
    const root = $("#fac-questions");
    root.innerHTML = "";
    const filters = [["unreviewed", "Needs review"], ["notfound", "Not found in docs"], ["negative", "👎 feedback"], ["all", "All questions"]];
    const chips = el("div", { class: "chips" }, filters.map(([v, l]) => el("button", { class: "chip", "aria-pressed": String(facQState.filter === v), onclick: () => { facQState.filter = v; facQuestions(); } }, l)));
    const search = el("input", { class: "input", type: "search", placeholder: "Search questions…", value: facQState.q, style: "max-width:280px" });
    let t;
    search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { facQState.q = search.value; loadQs(); }, 300); });
    root.append(
      el("div", { class: "callout" }, "Unanswered questions and 👎 answers usually mean a protocol is missing, outdated, or worded differently from how people ask. Fix the document (or add search keywords in Edit), then mark the question reviewed."),
      el("div", { class: "toolbar" }, chips, search, el("span", { class: "spacer" }), el("a", { class: "btn small", href: "api/admin/questions.csv" }, "Export CSV")));
    const list = el("div");
    root.append(list);
    async function loadQs() {
      let qs;
      try { qs = await api(`api/admin/questions?${new URLSearchParams({ filter: facQState.filter, q: facQState.q })}`); } catch (e) { toast(e.message, true); return; }
      list.innerHTML = "";
      if (!qs.length) { list.append(el("div", { class: "empty" }, facQState.filter === "unreviewed" ? "Nothing waiting for review. 🎉" : "No questions.")); return; }
      for (const q of qs) {
        const reviewed = el("button", { class: "btn small" }, "Mark reviewed");
        reviewed.addEventListener("click", async () => { await api(`api/admin/questions/${q.id}/reviewed`, { method: "POST" }); reviewed.replaceWith(el("span", { class: "badge ok" }, "Reviewed")); });
        list.append(el("div", { class: "card qitem" },
          el("div", { class: "row" },
            el("div", { class: "qtext", style: "flex:1;min-width:240px" }, q.question),
            q.found ? null : el("span", { class: "badge warn" }, "Not found"),
            q.feedback === -1 ? el("span", { class: "badge bad" }, "👎") : q.feedback === 1 ? el("span", { class: "badge ok" }, "👍") : null,
            q.reviewed ? el("span", { class: "badge ok" }, "Reviewed") : (q.found && q.feedback !== -1 ? null : reviewed)),
          el("div", { class: "muted small", style: "margin-top:2px" }, `${q.display_name || q.username || "deleted user"} · ${fmtDateTime(q.created_at)}`),
          q.feedback_note ? el("div", { class: "quote", style: "margin-top:8px" }, `Feedback: ${q.feedback_note}`) : null,
          el("details", {}, el("summary", {}, `Answer${q.sources.length ? ` · ${q.sources.length} source(s)` : ""}`),
            el("div", { class: "a-body", html: md(answerMarkdown(q.segments)) }),
            q.sources.length ? el("div", { class: "small muted", style: "margin-top:6px" }, q.sources.map((s) => `[${s.n}] ${s.title} (${s.locator})`).join(" · ")) : null)));
      }
    }
    loadQs();
  }

  // ---- users
  async function facUsers() {
    const root = $("#fac-users");
    root.innerHTML = "";
    const out = el("div");
    const uname = el("input", { class: "input", placeholder: "e.g. netid" });
    const dname = el("input", { class: "input", placeholder: "Full name" });
    const role = el("select", { class: "input" }, el("option", { value: "student" }, "Resident / intern"), el("option", { value: "faculty" }, "Faculty (can manage)"));
    const add = el("button", { class: "btn primary" }, "Add user");
    add.addEventListener("click", async () => {
      try {
        const r = await api("api/admin/users", { method: "POST", json: { username: uname.value, display_name: dname.value, role: role.value } });
        out.innerHTML = "";
        out.append(el("div", { class: "callout" }, "Created ", el("strong", {}, r.user.username), ". Temporary password: ", el("span", { class: "secret" }, r.temporary_password), " — share it privately; they'll set their own at first sign-in."));
        uname.value = ""; dname.value = "";
        loadUsers();
      } catch (e) { toast(e.message, true); }
    });
    const bulk = el("textarea", { class: "input", rows: 4, placeholder: "One per line:  netid, Full Name" });
    const bulkRole = el("select", { class: "input", style: "max-width:200px" }, el("option", { value: "student" }, "Resident / intern"), el("option", { value: "faculty" }, "Faculty"));
    const bulkBtn = el("button", { class: "btn" }, "Add all");
    bulkBtn.addEventListener("click", async () => {
      try {
        const r = await api("api/admin/users/bulk", { method: "POST", json: { lines: bulk.value, role: bulkRole.value } });
        out.innerHTML = "";
        const rows = r.created.map((c) => `${c.username}\t${c.display_name}\t${c.temporary_password}`).join("\n");
        out.append(el("div", { class: "callout" }, el("div", {}, el("strong", {}, `${r.created.length} created`), r.errors.length ? `, ${r.errors.length} skipped` : "", ". Copy these temporary passwords now — they won't be shown again."),
          el("textarea", { class: "input", rows: Math.min(10, r.created.length + 1), readonly: true, style: "font-family:var(--mono);font-size:12.5px;margin-top:6px" }, "username\tname\ttemporary password\n" + rows),
          r.errors.map((e) => el("div", { class: "small", style: "color:var(--bad)" }, `${e.line}: ${e.error}`))));
        bulk.value = "";
        loadUsers();
      } catch (e) { toast(e.message, true); }
    });
    root.append(
      el("div", { class: "grid2" },
        el("div", { class: "card", style: "padding:16px;margin-bottom:14px" }, el("div", { style: "font-weight:700;margin-bottom:10px" }, "Add one user"),
          el("label", { class: "field" }, "Username", uname), el("label", { class: "field" }, "Name", dname), el("label", { class: "field" }, "Role", role), add),
        el("div", { class: "card", style: "padding:16px;margin-bottom:14px" }, el("div", { style: "font-weight:700;margin-bottom:10px" }, "Add a class list"),
          bulk, el("div", { class: "row", style: "margin-top:10px" }, bulkRole, bulkBtn),
          el("div", { class: "muted small", style: "margin-top:8px" }, "Tip: use NetIDs as usernames so accounts carry over when sign-in moves to ISU NetID."))),
      out);
    const tbody = el("tbody");
    root.append(el("div", { class: "card table-wrap" }, el("table", { class: "data" },
      el("thead", {}, el("tr", {}, ["User", "Role", "Questions", "Last sign-in", "Active", ""].map((h) => el("th", {}, h)))), tbody)));
    async function loadUsers() {
      let users;
      try { users = await api("api/admin/users"); } catch (e) { toast(e.message, true); return; }
      tbody.innerHTML = "";
      for (const u of users) {
        const roleSel = el("select", { class: "input", style: "padding:4px 8px;max-width:170px" },
          el("option", { value: "student", selected: u.role === "student" }, "Resident / intern"), el("option", { value: "faculty", selected: u.role === "faculty" }, "Faculty"));
        roleSel.addEventListener("change", async () => {
          try { await api(`api/admin/users/${u.id}`, { method: "PATCH", json: { role: roleSel.value } }); toast("Role updated"); } catch (e) { toast(e.message, true); loadUsers(); }
        });
        const act = el("input", { type: "checkbox", checked: !!u.active });
        act.addEventListener("change", async () => {
          try { await api(`api/admin/users/${u.id}`, { method: "PATCH", json: { active: act.checked } }); toast(act.checked ? "Account enabled" : "Account disabled"); } catch (e) { toast(e.message, true); loadUsers(); }
        });
        const reset = el("button", { class: "btn small" }, "Reset password");
        reset.addEventListener("click", async () => {
          if (!confirm(`Reset password for ${u.username}? They'll be signed out.`)) return;
          try {
            const r = await api(`api/admin/users/${u.id}/reset-password`, { method: "POST" });
            out.innerHTML = "";
            out.append(el("div", { class: "callout" }, `New temporary password for ${u.username}: `, el("span", { class: "secret" }, r.temporary_password)));
            window.scrollTo({ top: 0, behavior: "smooth" });
          } catch (e) { toast(e.message, true); }
        });
        tbody.append(el("tr", {},
          el("td", {}, el("div", { style: "font-weight:600" }, u.display_name), el("div", { class: "small muted" }, u.username, u.must_change_pw ? " · temp password" : "")),
          el("td", {}, roleSel),
          el("td", { class: "small" }, u.questions),
          el("td", { class: "small" }, u.last_login ? fmtDateTime(u.last_login) : el("span", { class: "muted" }, "never")),
          el("td", {}, el("label", { class: "switch" }, act, el("span"))),
          el("td", {}, reset)));
      }
    }
    loadUsers();
  }

  // ---- activity
  async function facActivity() {
    const root = $("#fac-activity");
    root.innerHTML = "";
    let rows;
    try { rows = await api("api/admin/audit"); } catch (e) { toast(e.message, true); return; }
    const labels = { "document.add": "Added document", "document.replace": "Replaced file", "document.update": "Edited document",
      "document.delete": "Deleted document", "user.add": "Added user", "user.bulk_add": "Added users", "user.update": "Changed user",
      "user.reset_password": "Reset password" };
    root.append(el("div", { class: "card table-wrap" }, el("table", { class: "data" },
      el("thead", {}, el("tr", {}, ["When", "Who", "Action", "Detail"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.length ? rows.map((r) => el("tr", {}, el("td", { class: "small", style: "white-space:nowrap" }, fmtDateTime(r.created_at)),
        el("td", { class: "small" }, r.actor || ""), el("td", { class: "small" }, labels[r.action] || r.action), el("td", { class: "small muted" }, r.detail)))
        : el("tr", {}, el("td", { colspan: 4, class: "empty" }, "No activity yet."))))));
  }

  boot().catch((e) => { document.body.innerHTML = `<div class="empty">Couldn't load the app: ${esc(e.message)}</div>`; });
})();
