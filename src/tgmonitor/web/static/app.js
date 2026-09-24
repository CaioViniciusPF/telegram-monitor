"use strict";
// Frontend do tgmonitor. JS puro, sem dependências.
// Segurança: o API_TOKEN fica só nesta variável (memória); nada vai para
// localStorage/cookies. Todo dado vindo da API é inserido via textContent.

// Precisa bater com API_VERSION em web/api.py. Se o serviço rodando for mais
// antigo que esta interface (código atualizado sem reiniciar), avisamos.
const EXPECTED_API_VERSION = 2;

let token = null;
let rules = [];
let chats = [];
let editingRuleId = null;

const $ = (sel) => document.querySelector(sel);

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function brl(cents) {
  return (cents / 100).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
}

// "5.500,00" | "5500" | "5500,5" | "5500.50" -> centavos
function parseBRL(str) {
  let s = (str || "").trim().replace(/r\$\s*/i, "");
  if (!s) return null;
  if (s.includes(",")) s = s.replace(/\./g, "").replace(",", ".");
  else if (/^\d{1,3}(\.\d{3})+$/.test(s)) s = s.replace(/\./g, "");
  const v = Number(s);
  if (!Number.isFinite(v) || v <= 0) {
    throw new Error("Preço máximo inválido. Use um valor como 5.500,00 ou deixe em branco.");
  }
  return Math.round(v * 100);
}

function centsToInput(cents) {
  return cents == null ? "" : (cents / 100).toLocaleString("pt-BR", { minimumFractionDigits: 2 });
}

function whenText(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const dayDiff = Math.round(
    (new Date(now.getFullYear(), now.getMonth(), now.getDate()) -
      new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000
  );
  if (dayDiff === 0) return `hoje às ${time}`;
  if (dayDiff === 1) return `ontem às ${time}`;
  const opts = { day: "2-digit", month: "2-digit" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return `${d.toLocaleDateString("pt-BR", opts)} às ${time}`;
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function lines(text) {
  return text.split("\n").map((s) => s.trim()).filter(Boolean);
}

function listText(items) {
  // ["a", "b", "c"] -> "a, b e c"
  if (items.length <= 1) return items.join("");
  return items.slice(0, -1).join(", ") + " e " + items[items.length - 1];
}

let toastTimer;
function toast(msg, { error = false } = {}) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("is-error", error);
  t.setAttribute("role", error ? "alert" : "status");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), error ? 7000 : 3000);
}

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

const NETWORK_ERROR =
  "Sem conexão com o tgmonitor. Confira se o túnel SSH está aberto e se o serviço está rodando.";

async function api(method, path, body) {
  let res;
  try {
    res = await fetch("/api" + path, {
      method,
      headers: {
        "X-API-Token": token || "",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
      credentials: "omit",
    });
  } catch {
    throw new ApiError(NETWORK_ERROR);
  }
  if (res.status === 401) {
    token = null;
    askToken("Token inválido. Confira o valor de API_TOKEN e tente de novo.");
    throw new ApiError("não autorizado");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((d) => d.msg.replace(/^Value error, /, "")).join(". ");
    }
    throw new ApiError(
      detail || `O serviço respondeu com erro ${res.status}. Veja os logs com journalctl.`,
      res.status
    );
  }
  return data;
}

function showError(e, prefix) {
  if (e instanceof ApiError && e.message === "não autorizado") return;
  const msg = e.message || String(e);
  toast(prefix ? `${prefix}: ${msg}` : msg, { error: true });
}

// -------------------------------------------------------------------- token

function askToken(error) {
  const dlg = $("#token-dialog");
  $("#token-error").hidden = !error;
  $("#token-error").textContent = error || "";
  $("#token-input").value = "";
  if (!dlg.open) dlg.showModal();
}

$("#token-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  token = $("#token-input").value.trim();
  try {
    await api("GET", "/health");
    $("#token-dialog").close();
    await boot();
  } catch (e) {
    if (!(e instanceof ApiError) || e.message !== "não autorizado") askToken(e.message);
  }
});
$("#token-dialog").addEventListener("cancel", (ev) => ev.preventDefault());

// --------------------------------------------------------------------- tabs

const loaders = {
  chats: loadChats,
  rules: loadRules,
  test: loadTestRules,
  alerts: loadAlerts,
  settings: loadSettings,
};

document.querySelectorAll("#tabs button").forEach((btn) =>
  btn.addEventListener("click", () => showTab(btn.dataset.tab))
);

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => {
    if (b.dataset.tab === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + name));
  loaders[name]().catch((e) => showError(e));
}

// ------------------------------------------------------------------- status

async function refreshStatus() {
  if (!token) return null;
  const pill = $("#status");
  try {
    const h = await api("GET", "/health");
    $("#version-banner").hidden = h.api_version === EXPECTED_API_VERSION;
    const l = h.listener || {};
    if (h.paused) {
      pill.textContent = "Alertas pausados";
      pill.className = "status bad";
    } else if (l.healthy) {
      pill.textContent = "Conectado";
      pill.className = "status ok";
    } else {
      const labels = { connecting: "Conectando", reconnecting: "Reconectando", fatal: "Sessão inválida", starting: "Iniciando" };
      pill.textContent = labels[l.state] || "Com problemas";
      pill.className = "status bad";
    }
    return h;
  } catch (e) {
    pill.textContent = "Sem conexão";
    pill.className = "status bad";
    return null;
  }
}

// -------------------------------------------------------------------- chats

async function loadChats() {
  chats = await api("GET", "/chats");
  renderChats();
}

function renderChats() {
  const q = $("#chat-search").value.trim().toLowerCase();
  const only = $("#chat-only-monitored").checked;
  const list = $("#chat-list");
  list.replaceChildren();
  const kinds = { channel: "canal", megagroup: "supergrupo", group: "grupo" };
  const visible = chats.filter(
    (c) =>
      (!only || c.monitored) &&
      (!q || c.title.toLowerCase().includes(q) || (c.username || "").toLowerCase().includes(q))
  );
  for (const c of visible) {
    const cb = el("input", { type: "checkbox" });
    cb.checked = c.monitored;
    cb.addEventListener("change", async () => {
      cb.disabled = true;
      try {
        await api("PUT", `/chats/${c.id}`, { monitored: cb.checked });
        c.monitored = cb.checked;
        updateChatSummary();
      } catch (e) {
        cb.checked = !cb.checked;
        showError(e, "Não consegui alterar o monitoramento");
      } finally {
        cb.disabled = false;
      }
    });
    list.append(
      el("li", { class: "chat-row" },
        el("label", { class: "check" }, cb,
          el("span", { class: "chat-title" }, c.title),
          c.username ? el("span", { class: "chat-user" }, "@" + c.username) : null),
        el("span", { class: "chat-kind" }, kinds[c.kind] || c.kind))
    );
  }
  list.hidden = !visible.length;
  let empty = $("#chat-empty");
  if (!empty) {
    empty = el("p", { id: "chat-empty", class: "empty" });
    list.after(empty);
  }
  empty.hidden = visible.length > 0;
  if (!chats.length) {
    empty.textContent =
      "Nenhum grupo carregado. Clique em Atualizar grupos para buscar os grupos e canais da sua conta.";
  } else if (only && !q) {
    empty.textContent = "Nenhum grupo monitorado ainda. Desmarque “Só monitorados” e marque os grupos que quer acompanhar.";
  } else {
    empty.textContent = `Nenhum grupo corresponde a “${$("#chat-search").value.trim()}”.`;
  }
  updateChatSummary();
}

function updateChatSummary() {
  const n = chats.filter((c) => c.monitored).length;
  $("#chat-summary").textContent = chats.length
    ? `${n} de ${plural(chats.length, "grupo ou canal", "grupos e canais")} monitorados.`
    : "";
}

$("#chat-search").addEventListener("input", renderChats);
$("#chat-only-monitored").addEventListener("change", renderChats);
$("#chat-refresh").addEventListener("click", async () => {
  const btn = $("#chat-refresh");
  btn.disabled = true;
  btn.textContent = "Atualizando grupos";
  try {
    const r = await api("POST", "/chats/refresh");
    await loadChats();
    toast(`Grupos atualizados: ${plural(r.count, "encontrado", "encontrados")}.`);
  } catch (e) {
    const msg = e.status === 503
      ? "Não consegui atualizar os grupos: o listener está desconectado. Aguarde a reconexão ou envie /status ao bot."
      : e.message;
    toast(msg, { error: true });
  } finally {
    btn.disabled = false;
    btn.textContent = "Atualizar grupos";
  }
});

// -------------------------------------------------------------------- rules

function chipList(terms, cls) {
  return el("span", { class: "chips" }, terms.map((t) => el("span", { class: "chip " + cls }, t)));
}

async function loadRules() {
  rules = await api("GET", "/rules");
  const list = $("#rule-list");
  list.replaceChildren();
  for (const r of rules) list.append(ruleRow(r));
  list.hidden = !rules.length;
  let empty = $("#rule-empty");
  if (!empty) {
    empty = el("p", { id: "rule-empty", class: "empty" },
      "Nenhuma regra ainda. Crie a primeira para começar a receber alertas.");
    list.after(empty);
  }
  empty.hidden = rules.length > 0;
}

function ruleRow(r) {
  const toggle = el("input", { type: "checkbox", "aria-label": `Regra “${r.name}” ativa` });
  toggle.checked = r.enabled;
  const name = el("span", { class: "rule-name" + (r.enabled ? "" : " off") }, r.name);
  toggle.addEventListener("change", async () => {
    toggle.disabled = true;
    try {
      await api("PUT", `/rules/${r.id}`, { ...ruleBody(r), enabled: toggle.checked });
      r.enabled = toggle.checked;
      name.className = "rule-name" + (r.enabled ? "" : " off");
      toast(r.enabled ? "Regra ativada." : "Regra desativada.");
    } catch (e) {
      toggle.checked = !toggle.checked;
      showError(e, "Não consegui alterar a regra");
    } finally {
      toggle.disabled = false;
    }
  });

  const actions = el("div", { class: "actions" },
    el("button", { type: "button", class: "btn-quiet", onclick: () => openRuleForm(r) }, "Editar"),
    el("button", { type: "button", class: "btn-quiet danger", onclick: () => askDelete() }, "Excluir"));
  const confirm = el("div", { class: "confirm", hidden: true },
    el("p", {}, `Excluir a regra “${r.name}”?`),
    el("button", { type: "button", class: "btn btn-danger", onclick: () => deleteRule(r) }, "Excluir regra"),
    el("button", { type: "button", class: "btn-quiet", onclick: () => cancelDelete() }, "Manter"));

  function askDelete() {
    actions.hidden = true;
    confirm.hidden = false;
    confirm.querySelector(".btn-danger").focus();
  }
  function cancelDelete() {
    confirm.hidden = true;
    actions.hidden = false;
    actions.querySelector(".danger").focus();
  }

  return el("li", { class: "rule-row" },
    toggle,
    name,
    el("span", { class: "rule-price" },
      r.max_price_cents == null ? el("span", { class: "meta" }, "sem preço máximo") : `até ${brl(r.max_price_cents)}`),
    el("div", { class: "rule-terms" },
      el("div", { class: "term-line" }, el("span", { class: "meta" }, "Inclui"), chipList(r.include_terms, "inc")),
      el("div", { class: "term-line" }, el("span", { class: "meta" }, "Exclui"),
        r.exclude_terms.length ? chipList(r.exclude_terms, "exc") : el("span", { class: "meta" }, "nada"))),
    actions,
    confirm);
}

function ruleBody(r) {
  return {
    name: r.name,
    include_terms: r.include_terms,
    exclude_terms: r.exclude_terms,
    max_price_cents: r.max_price_cents,
    enabled: r.enabled,
  };
}

function openRuleForm(rule) {
  const f = $("#rule-form");
  editingRuleId = rule ? rule.id : null;
  $("#rule-form-title").textContent = rule ? `Editar regra “${rule.name}”` : "Nova regra";
  f.elements.name.value = rule ? rule.name : "";
  f.elements.include.value = rule ? rule.include_terms.join("\n") : "";
  f.elements.exclude.value = rule ? rule.exclude_terms.join("\n") : "";
  f.elements.max_price.value = rule ? centsToInput(rule.max_price_cents) : "";
  f.elements.enabled.checked = rule ? rule.enabled : true;
  $("#rule-error").hidden = true;
  $("#draft-result").replaceChildren();
  f.hidden = false;
  f.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  f.elements.name.focus({ preventScroll: true });
}

function formDraft() {
  const f = $("#rule-form");
  const draft = {
    name: f.elements.name.value.trim(),
    include_terms: lines(f.elements.include.value),
    exclude_terms: lines(f.elements.exclude.value),
    max_price_cents: parseBRL(f.elements.max_price.value),
    enabled: f.elements.enabled.checked,
  };
  return draft;
}

$("#rule-new").addEventListener("click", () => openRuleForm(null));
$("#rule-cancel").addEventListener("click", () => {
  $("#rule-form").hidden = true;
  $("#rule-new").focus();
});

$("#rule-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = $("#rule-error");
  err.hidden = true;
  try {
    const body = formDraft();
    if (!body.name) throw new Error("Dê um nome à regra.");
    if (!body.include_terms.length) throw new Error("Informe ao menos um termo de inclusão, um por linha.");
    if (editingRuleId == null) await api("POST", "/rules", body);
    else await api("PUT", `/rules/${editingRuleId}`, body);
    $("#rule-form").hidden = true;
    toast("Regra salva.");
    await loadRules();
  } catch (e) {
    if (e instanceof ApiError && e.message === "não autorizado") return;
    err.textContent = `Não consegui salvar a regra: ${e.message}`;
    err.hidden = false;
  }
});

async function deleteRule(r) {
  try {
    await api("DELETE", `/rules/${r.id}`);
    toast("Regra excluída.");
    await loadRules();
  } catch (e) {
    showError(e, "Não consegui excluir a regra");
  }
}

$("#draft-test").addEventListener("click", async () => {
  const out = $("#draft-result");
  const text = $("#draft-text").value;
  if (!text.trim()) {
    out.replaceChildren(el("p", { class: "error" }, "Cole um texto para testar o rascunho."));
    return;
  }
  try {
    const draft = formDraft();
    draft.name = draft.name || "Rascunho";
    if (!draft.include_terms.length) throw new Error("Informe ao menos um termo de inclusão antes de testar.");
    const res = await api("POST", "/rules/test", { text, draft });
    out.replaceChildren(renderTestResult(text, res));
  } catch (e) {
    if (e instanceof ApiError && e.message === "não autorizado") return;
    out.replaceChildren(el("p", { class: "error" }, e.message));
  }
});

// --------------------------------------------------------------------- test

async function loadTestRules() {
  rules = await api("GET", "/rules");
  const sel = $("#test-rule");
  const current = sel.value;
  sel.replaceChildren(el("option", { value: "" }, "Todas as regras"));
  for (const r of rules) {
    sel.append(el("option", { value: String(r.id) }, r.name + (r.enabled ? "" : " (inativa)")));
  }
  sel.value = current;
}

$("#test-run").addEventListener("click", async () => {
  const out = $("#test-result");
  const text = $("#test-text").value;
  if (!text.trim()) {
    out.replaceChildren(el("p", { class: "error" }, "Cole um texto para testar."));
    return;
  }
  const ruleId = $("#test-rule").value;
  try {
    const res = await api("POST", "/rules/test", { text, rule_id: ruleId ? Number(ruleId) : null });
    out.replaceChildren(renderTestResult(text, res));
  } catch (e) {
    if (e instanceof ApiError && e.message === "não autorizado") return;
    out.replaceChildren(el("p", { class: "error" }, `Não consegui testar: ${e.message}`));
  }
});

// Preenche campos ausentes: um serviço antigo (não reiniciado após atualizar o
// código) responde sem posições e motivos. Nesse caso o aviso de versão aparece
// e o resultado sai sem destaques, em vez de quebrar.
function normalizeTestResult(res) {
  const candidates = (res.price_candidates || []).map((c) => ({
    reason: "", start: null, end: null, used: false, ...c,
  }));
  if (res.price_cents != null && !candidates.some((c) => c.used)) {
    const guess = candidates.find((c) => c.cents === res.price_cents && c.kind !== "discard");
    if (guess) guess.used = true;
  }
  return {
    ...res,
    price_candidates: candidates,
    results: (res.results || []).map((r) => ({
      included_by: [], excluded_by: [], include_spans: [], exclude_spans: [], ...r,
    })),
  };
}

// Resultado do teste: decisão, texto anotado, valores e regras.
function renderTestResult(text, rawRes) {
  const res = normalizeTestResult(rawRes);
  if (!res.results.length) {
    return el("p", { class: "empty" }, "Nenhuma regra para testar. Crie uma regra na aba Regras.");
  }
  const used = res.price_candidates.find((c) => c.used) || null;
  return el("div", { class: "result" },
    renderDecision(res, used),
    el("div", { class: "result-body" },
      renderAnnotated(text, res),
      res.price_candidates.length ? renderValues(res.price_candidates, used) : null,
      renderRuleResults(res)));
}

function priceLabel(c) {
  return c.reason && c.reason !== "sem contexto" ? `${brl(c.cents)} ${c.reason}` : brl(c.cents);
}

// Regra salva e desativada (rascunho tem id 0 e conta como ativa).
function isInactive(r) {
  const saved = rules.find((x) => x.id === r.rule_id);
  return saved ? !saved.enabled : false;
}

function renderDecision(res, used) {
  const active = res.results.filter((r) => !isInactive(r));
  const matched = active.filter((r) => r.matched);
  if (matched.length) {
    const price = used ? priceLabel(used) : "preço não identificado";
    const detail = `${matched.length} de ${plural(active.length, "regra ativa", "regras ativas")}, ${price}`;
    return el("div", { class: "result-decision yes" },
      el("p", {}, el("span", { class: "verdict" }, "Alertaria: "), el("span", { class: "detail" }, detail)));
  }
  const inactiveMatch = res.results.find((r) => r.matched && isInactive(r));
  const reason = inactiveMatch && !active.some((r) => r.excluded_by.length || r.price_over_limit)
    ? `só casou com ${inactiveMatch.rule_name}, que está inativa. Ative a regra para receber alertas`
    : mainReason({ ...res, results: active.length ? active : res.results });
  return el("div", { class: "result-decision no" },
    el("p", {}, el("span", { class: "verdict" }, "Não alertaria: "),
      el("span", { class: "detail" }, reason)));
}

function mainReason(res) {
  const excluded = res.results.find((r) => r.excluded_by.length);
  if (excluded) {
    const terms = listText(excluded.excluded_by.map((t) => `“${t}”`));
    return `${excluded.excluded_by.length > 1 ? "termos de exclusão" : "termo de exclusão"} ${terms} em ${excluded.rule_name}`;
  }
  const over = res.results.find((r) => r.price_over_limit);
  if (over) {
    return `${brl(res.price_cents)} está acima do máximo de ${brl(over.max_price_cents)} em ${over.rule_name}`;
  }
  return res.results.length > 1 ? "nenhuma regra teve termo de inclusão no texto" : "nenhum termo de inclusão está no texto";
}

// Marca cada caractere (em code points, como o Python) e agrupa em trechos.
function renderAnnotated(text, res) {
  const cps = Array.from(text);
  const marks = cps.map(() => ({ inc: [], exc: [], price: null }));
  const put = (start, end, fn) => {
    if (start == null || end == null) return;
    for (let i = Math.max(0, start); i < Math.min(end, cps.length); i++) fn(marks[i]);
  };
  for (const r of res.results) {
    for (const s of r.include_spans) put(s.start, s.end, (m) => m.inc.push(s.term));
    for (const s of r.exclude_spans) put(s.start, s.end, (m) => m.exc.push(s.term));
  }
  for (const c of res.price_candidates) {
    const state = c.used ? "used" : c.kind === "discard" ? "ign" : "other";
    put(c.start, c.end, (m) => (m.price = { state, c }));
  }

  const box = el("div", { class: "analyzed" });
  let i = 0;
  let any = { inc: false, exc: false, used: false, ign: false, other: false };
  while (i < cps.length) {
    const key = markKey(marks[i]);
    let j = i + 1;
    while (j < cps.length && markKey(marks[j]) === key) j++;
    const chunk = cps.slice(i, j).join("");
    const m = marks[i];
    if (!key) {
      box.append(chunk);
    } else {
      const cls = [];
      const title = [];
      if (m.inc.length) { cls.push("hl-inc"); any.inc = true; title.push(`inclusão: ${[...new Set(m.inc)].join(", ")}`); }
      if (m.exc.length) { cls.push("hl-exc"); any.exc = true; title.push(`exclusão: ${[...new Set(m.exc)].join(", ")}`); }
      if (m.price) {
        cls.push("hl-" + m.price.state);
        any[m.price.state] = true;
        const label = { used: "valor usado", ign: "valor ignorado", other: "valor não usado" }[m.price.state];
        title.push(`${label}: ${m.price.c.reason}`);
      }
      box.append(el(m.inc.length ? "mark" : "span", { class: cls.join(" "), title: title.join("; ") }, chunk));
    }
    i = j;
  }

  const legendItems = [
    any.inc && el("span", {}, el("mark", { class: "hl-inc" }, "Inclusão")),
    any.exc && el("span", {}, el("span", { class: "hl-exc" }, "Exclusão")),
    any.used && el("span", {}, el("span", { class: "hl-used" }, "Valor usado")),
    any.other && el("span", {}, el("span", { class: "hl-other" }, "Valor não usado")),
    any.ign && el("span", {}, el("span", { class: "hl-ign" }, "Valor ignorado")),
  ].filter(Boolean);

  return el("div", {},
    el("h3", {}, "Texto analisado"),
    legendItems.length ? el("p", { class: "legend", "aria-label": "Legenda" }, legendItems) : null,
    box);
}

function markKey(m) {
  if (!m.inc.length && !m.exc.length && !m.price) return "";
  return `${m.inc.join("|")}#${m.exc.join("|")}#${m.price ? m.price.state + m.price.c.start : ""}`;
}

const KIND_RANK = { discard: 0, neutral: 1, por: 2, final: 3 };

function renderValues(candidates, used) {
  const rows = candidates.map((c) => {
    let state, why;
    if (c.used) {
      state = el("span", { class: "state-used" }, "usado");
      why = c.reason === "sem contexto" ? "menor valor encontrado" : c.reason;
    } else if (c.kind === "discard") {
      state = el("span", { class: "state-ignored" }, "ignorado");
      why = c.reason;
    } else {
      state = el("span", { class: "state-unused" }, "não usado");
      if (!used) {
        why = c.reason;
      } else {
        const usedLabel = !used.reason || used.reason === "sem contexto" ? "o valor usado" : `“${used.reason}”`;
        why = KIND_RANK[c.kind] === KIND_RANK[used.kind]
          ? "maior que o valor usado"
          : c.reason ? `${c.reason}: prioridade menor que ${usedLabel}` : `prioridade menor que ${usedLabel}`;
      }
    }
    return el("tr", {},
      el("td", { class: "num" }, brl(c.cents)),
      el("td", {}, state, el("span", { class: "meta why-mobile" }, why)),
      el("td", {}, why));
  });
  return el("div", {},
    el("h3", {}, "Valores encontrados"),
    el("table", { class: "values" },
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Valor"), el("th", { scope: "col" }, "Situação"), el("th", { scope: "col" }, "Motivo"))),
      el("tbody", {}, rows)));
}

function renderRuleResults(res) {
  const outcome = (r) => {
    if (r.matched) {
      return r.max_price_cents != null && res.price_cents == null ? "casou, preço não identificado" : "casou";
    }
    if (r.excluded_by.length) return "não casou: termo de exclusão no texto";
    if (r.price_over_limit) return `não casou: preço acima de ${brl(r.max_price_cents)}`;
    return "não casou: nenhum termo de inclusão no texto";
  };
  return el("div", {},
    el("h3", {}, res.results.length > 1 ? "Por regra" : "Regra"),
    el("ul", { class: "rule-results" },
      res.results.map((r) =>
        el("li", { class: r.matched ? "yes" : "no" },
          el("p", {}, el("strong", {}, r.rule_name),
            isInactive(r) ? el("span", { class: "meta" }, " (inativa)") : null,
            " ", el("span", { class: "outcome" }, outcome(r))),
          r.included_by.length
            ? el("div", { class: "term-line" }, el("span", { class: "meta" }, "Inclusão"), chipList(r.included_by, "inc"))
            : null,
          r.excluded_by.length
            ? el("div", { class: "term-line" }, el("span", { class: "meta" }, "Exclusão"), chipList(r.excluded_by, "exc"))
            : null))));
}

// ------------------------------------------------------------------- alerts

async function loadAlerts() {
  const alerts = await api("GET", "/alerts?limit=100");
  const list = $("#alert-list");
  list.replaceChildren();
  for (const a of alerts) list.append(alertRow(a));
  list.hidden = !alerts.length;
  let empty = $("#alert-empty");
  if (!empty) {
    empty = el("p", { id: "alert-empty", class: "empty" },
      "Nenhum alerta ainda. Quando uma mensagem de um grupo monitorado casar com uma regra ativa, ela aparece aqui.");
    list.after(empty);
  }
  empty.hidden = alerts.length > 0;
}

function alertRow(a) {
  // Só links t.me são exibidos como link clicável.
  const safeLink = a.link && a.link.startsWith("https://t.me/") ? a.link : null;
  const rulesText = a.matched_rules.map((r) => r.name);
  const meta = [
    `${rulesText.length > 1 ? "Regras" : "Regra"} ${listText(rulesText)}`,
    whenText(a.created_at),
    a.previous_price_cents != null ? "preço baixou após edição do post" : a.is_edit ? "mensagem editada" : null,
  ].filter(Boolean).join(", ");

  const text = el("p", { class: "alert-text" }, a.text);
  const long = a.text.length > 180 || a.text.split("\n").length > 2;
  const toggle = long
    ? el("button", { type: "button", class: "btn-quiet", "aria-expanded": "false" }, "Mostrar mensagem inteira")
    : null;
  if (toggle) {
    toggle.addEventListener("click", () => {
      const open = text.classList.toggle("expanded");
      toggle.setAttribute("aria-expanded", String(open));
      toggle.textContent = open ? "Recolher mensagem" : "Mostrar mensagem inteira";
    });
  }

  return el("li", { class: "alert-row" + (a.delivered ? "" : " undelivered") },
    a.price_cents == null
      ? el("p", { class: "alert-price none" }, "Preço não identificado")
      : el("div", {},
          el("p", { class: "alert-price" }, brl(a.price_cents)),
          a.previous_price_cents != null
            ? el("p", { class: "price-drop" }, `Baixou de ${brl(a.previous_price_cents)}`)
            : null),
    el("div", {},
      el("p", { class: "alert-group" }, a.chat_title),
      el("p", { class: "meta" }, meta + "."),
      a.delivered
        ? null
        : el("p", { class: "undelivered-note" },
            "Não entregue: o bot não conseguiu enviar este alerta. Confira se você enviou /start ao bot e veja os logs do serviço."),
      text,
      el("div", { class: "alert-links" },
        toggle,
        safeLink ? el("a", { href: safeLink, target: "_blank", rel: "noopener noreferrer" }, "Abrir no Telegram") : null)));
}

$("#alerts-reload").addEventListener("click", async () => {
  try {
    await loadAlerts();
    toast("Histórico recarregado.");
  } catch (e) {
    showError(e, "Não consegui recarregar o histórico");
  }
});

// ----------------------------------------------------------------- settings

async function loadSettings() {
  const s = await api("GET", "/settings");
  const f = $("#settings-form");
  for (const [k, v] of Object.entries(s)) {
    const input = f.elements[k];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = v;
    else input.value = v;
  }
  renderHealth(await refreshStatus());
}

function renderHealth(h) {
  const out = $("#health-summary");
  if (!h) {
    out.textContent = NETWORK_ERROR;
    $("#health").textContent = "";
    return;
  }
  const l = h.listener || {};
  const parts = [];
  parts.push(l.healthy ? "Listener conectado." : `Listener com problemas (${l.state || "desconhecido"}). Envie /status ao bot para mais detalhes.`);
  if (h.paused) parts.push("Alertas pausados.");
  parts.push(`${plural(h.active_rules ?? 0, "regra ativa", "regras ativas")} e ${plural(h.monitored_chats ?? 0, "grupo monitorado", "grupos monitorados")}.`);
  if (h.last_message_at) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - h.last_message_at) / 60));
    parts.push(mins < 1 ? "Última mensagem monitorada agora há pouco." : `Última mensagem monitorada há ${plural(mins, "minuto", "minutos")}.`);
  } else {
    parts.push("Nenhuma mensagem monitorada desde que o serviço iniciou.");
  }
  out.textContent = parts.join(" ");
  $("#health").textContent = JSON.stringify(h, null, 2);
}

$("#settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const err = $("#settings-error");
  err.hidden = true;
  const body = {
    paused: f.elements.paused.checked,
    dedup_window_hours: Number(f.elements.dedup_window_hours.value),
    max_new_message_age_hours: Number(f.elements.max_new_message_age_hours.value),
    max_edit_age_hours: Number(f.elements.max_edit_age_hours.value),
    alert_retention_days: Number(f.elements.alert_retention_days.value),
  };
  try {
    await api("PUT", "/settings", body);
    toast("Ajustes salvos.");
    renderHealth(await refreshStatus());
  } catch (e) {
    if (e instanceof ApiError && e.message === "não autorizado") return;
    err.textContent = `Não consegui salvar os ajustes: ${e.message}`;
    err.hidden = false;
  }
});

// --------------------------------------------------------------------- boot

let statusTimer;
async function boot() {
  await refreshStatus();
  clearInterval(statusTimer);
  statusTimer = setInterval(refreshStatus, 30000);
  showTab("chats");
}

askToken();
