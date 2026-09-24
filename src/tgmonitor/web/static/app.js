"use strict";
// Frontend do tgmonitor. JS puro, sem dependências.
// Segurança: o API_TOKEN fica só nesta variável (memória); nada vai para
// localStorage/cookies. Todo dado vindo da API é inserido via textContent.

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
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function brl(cents) {
  if (cents == null) return "—";
  return (cents / 100).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
}

// "5.500,00" | "5500" | "5500,5" | "5500.50" -> centavos
function parseBRL(str) {
  let s = (str || "").trim().replace(/r\$\s*/i, "");
  if (!s) return null;
  if (s.includes(",")) s = s.replace(/\./g, "").replace(",", ".");
  else if (/^\d{1,3}(\.\d{3})+$/.test(s)) s = s.replace(/\./g, "");
  const v = Number(s);
  if (!Number.isFinite(v) || v <= 0) throw new Error("Preço máximo inválido");
  return Math.round(v * 100);
}

function centsToInput(cents) {
  return cents == null ? "" : (cents / 100).toLocaleString("pt-BR", { minimumFractionDigits: 2 });
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString("pt-BR");
}

function lines(text) {
  return text.split("\n").map((s) => s.trim()).filter(Boolean);
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2500);
}

class ApiError extends Error {}

async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: {
      "X-API-Token": token || "",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
    credentials: "omit",
  });
  if (res.status === 401) {
    token = null;
    askToken("Token inválido.");
    throw new ApiError("não autorizado");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    }
    throw new ApiError(detail || `erro ${res.status}`);
  }
  return data;
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
    if (!(e instanceof ApiError)) askToken("Falha ao conectar: " + e.message);
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
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name)
  );
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + name));
  loaders[name]().catch(showError);
}

function showError(e) {
  if (!(e instanceof ApiError) || e.message !== "não autorizado") toast("Erro: " + e.message);
}

// ------------------------------------------------------------------- status

async function refreshStatus() {
  if (!token) return;
  try {
    const h = await api("GET", "/health");
    const pill = $("#status-pill");
    const l = h.listener;
    if (h.paused) {
      pill.textContent = "pausado";
      pill.className = "pill warn";
    } else if (l.healthy) {
      pill.textContent = "conectado";
      pill.className = "pill ok";
    } else {
      pill.textContent = l.state;
      pill.className = "pill bad";
    }
    return h;
  } catch (e) {
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
    (c) => (!only || c.monitored) && (!q || c.title.toLowerCase().includes(q) || (c.username || "").toLowerCase().includes(q))
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
        showError(e);
      } finally {
        cb.disabled = false;
      }
    });
    list.append(
      el("li", {},
        el("label", { class: "check" }, cb, c.title),
        c.username ? el("span", { class: "muted" }, "@" + c.username) : null,
        el("span", { class: "kind" }, kinds[c.kind] || c.kind))
    );
  }
  if (!visible.length) {
    list.append(el("li", { class: "muted" },
      chats.length ? "Nada encontrado." : "Nenhum chat. Clique em “Atualizar lista”."));
  }
  updateChatSummary();
}

function updateChatSummary() {
  const n = chats.filter((c) => c.monitored).length;
  $("#chat-summary").textContent = `${chats.length} grupos/canais · ${n} monitorados`;
}

$("#chat-search").addEventListener("input", renderChats);
$("#chat-only-monitored").addEventListener("change", renderChats);
$("#chat-refresh").addEventListener("click", async () => {
  const btn = $("#chat-refresh");
  btn.disabled = true;
  try {
    const r = await api("POST", "/chats/refresh");
    toast(`${r.count} grupos/canais encontrados`);
    await loadChats();
  } catch (e) {
    showError(e);
  } finally {
    btn.disabled = false;
  }
});

// -------------------------------------------------------------------- rules

async function loadRules() {
  rules = await api("GET", "/rules");
  const tbody = $("#rule-rows");
  tbody.replaceChildren();
  for (const r of rules) {
    const toggle = el("input", { type: "checkbox" });
    toggle.checked = r.enabled;
    toggle.addEventListener("change", async () => {
      try {
        await api("PUT", `/rules/${r.id}`, { ...ruleBody(r), enabled: toggle.checked });
        r.enabled = toggle.checked;
      } catch (e) {
        toggle.checked = !toggle.checked;
        showError(e);
      }
    });
    tbody.append(
      el("tr", {},
        el("td", {}, toggle),
        el("td", {}, r.name),
        el("td", { class: "terms" }, r.include_terms.join("\n")),
        el("td", { class: "terms" }, r.exclude_terms.join("\n") || "—"),
        el("td", {}, brl(r.max_price_cents)),
        el("td", { class: "actions" },
          el("button", { onclick: () => openRuleForm(r) }, "Editar"), " ",
          el("button", { class: "danger", onclick: () => deleteRule(r) }, "Excluir")))
    );
  }
  if (!rules.length) {
    tbody.append(el("tr", {}, el("td", { colspan: "6", class: "muted" }, "Nenhuma regra ainda.")));
  }
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
  $("#rule-form-title").textContent = rule ? `Editar: ${rule.name}` : "Nova regra";
  f.elements.name.value = rule ? rule.name : "";
  f.include.value = rule ? rule.include_terms.join("\n") : "";
  f.exclude.value = rule ? rule.exclude_terms.join("\n") : "";
  f.max_price.value = rule ? centsToInput(rule.max_price_cents) : "";
  f.enabled.checked = rule ? rule.enabled : true;
  $("#rule-error").hidden = true;
  $("#draft-result").replaceChildren();
  f.hidden = false;
  f.scrollIntoView({ behavior: "smooth" });
  f.elements.name.focus();
}

function formDraft() {
  const f = $("#rule-form");
  return {
    name: f.elements.name.value.trim() || "rascunho",
    include_terms: lines(f.include.value),
    exclude_terms: lines(f.exclude.value),
    max_price_cents: parseBRL(f.max_price.value),
    enabled: f.enabled.checked,
  };
}

$("#rule-new").addEventListener("click", () => openRuleForm(null));
$("#rule-cancel").addEventListener("click", () => ($("#rule-form").hidden = true));

$("#rule-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = $("#rule-error");
  err.hidden = true;
  try {
    const body = formDraft();
    if (editingRuleId == null) await api("POST", "/rules", body);
    else await api("PUT", `/rules/${editingRuleId}`, body);
    $("#rule-form").hidden = true;
    toast("Regra salva");
    await loadRules();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
});

async function deleteRule(r) {
  if (!confirm(`Excluir a regra “${r.name}”?`)) return;
  try {
    await api("DELETE", `/rules/${r.id}`);
    toast("Regra excluída");
    await loadRules();
  } catch (e) {
    showError(e);
  }
}

$("#draft-test").addEventListener("click", async () => {
  const out = $("#draft-result");
  try {
    const text = $("#draft-text").value;
    if (!text.trim()) return;
    const res = await api("POST", "/rules/test", { text, draft: formDraft() });
    out.replaceChildren(renderTestResult(res));
  } catch (e) {
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
  if (!text.trim()) return;
  const ruleId = $("#test-rule").value;
  try {
    const res = await api("POST", "/rules/test", {
      text,
      rule_id: ruleId ? Number(ruleId) : null,
    });
    out.replaceChildren(renderTestResult(res));
  } catch (e) {
    out.replaceChildren(el("p", { class: "error" }, e.message));
  }
});

function renderTestResult(res) {
  const labels = { final: "preço final", por: "“por”", neutral: "neutro", discard: "descartado" };
  const box = el("div", { class: "result card" });
  box.append(
    el("p", {}, el("strong", {}, "Preço extraído: "),
      res.price_cents == null ? "não identificado" : brl(res.price_cents))
  );
  if (res.price_candidates.length) {
    box.append(el("p", {}, ...res.price_candidates.map((c) =>
      el("span", { class: "tag " + c.kind, title: labels[c.kind] }, `${brl(c.cents)} · ${labels[c.kind]}`))));
  }
  if (!res.results.length) box.append(el("p", { class: "muted" }, "Nenhuma regra para testar."));
  for (const r of res.results) {
    box.append(
      el("div", { class: "rule " + (r.matched ? "match" : "nomatch") },
        el("strong", {}, (r.matched ? "✅ " : "❌ ") + r.rule_name), " — ", r.reason,
        r.included_by.length ? el("div", {}, "Inclusão: ", ...r.included_by.map((t) => el("span", { class: "tag" }, t))) : null,
        r.excluded_by.length ? el("div", {}, "Exclusão: ", ...r.excluded_by.map((t) => el("span", { class: "tag" }, t))) : null)
    );
  }
  return box;
}

// ------------------------------------------------------------------- alerts

async function loadAlerts() {
  const alerts = await api("GET", "/alerts?limit=100");
  const list = $("#alert-list");
  list.replaceChildren();
  for (const a of alerts) {
    // Só links t.me são exibidos como link clicável.
    const safeLink = a.link && a.link.startsWith("https://t.me/") ? a.link : null;
    list.append(
      el("li", {},
        el("div", { class: "meta" },
          el("span", { class: "rules-names" }, a.matched_rules.map((r) => r.name).join(", ")),
          el("span", {}, a.chat_title),
          el("span", {}, brl(a.price_cents)),
          el("span", {}, fmtDate(a.created_at)),
          a.is_edit ? el("span", {}, "✏️ editada") : null,
          a.delivered ? null : el("span", { class: "error" }, "não entregue")),
        el("div", { class: "text" }, a.text),
        safeLink ? el("a", { href: safeLink, target: "_blank", rel: "noopener noreferrer" }, "Abrir no Telegram") : null)
    );
  }
  if (!alerts.length) list.append(el("li", { class: "muted" }, "Nenhum alerta ainda."));
}

$("#alerts-reload").addEventListener("click", () => loadAlerts().catch(showError));

// ----------------------------------------------------------------- settings

async function loadSettings() {
  const s = await api("GET", "/settings");
  const f = $("#settings-form");
  for (const [k, v] of Object.entries(s)) {
    if (!f[k]) continue;
    if (f[k].type === "checkbox") f[k].checked = v;
    else f[k].value = v;
  }
  const h = await refreshStatus();
  $("#health").textContent = h ? JSON.stringify(h, null, 2) : "indisponível";
}

$("#settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const body = {
    paused: f.paused.checked,
    dedup_window_hours: Number(f.dedup_window_hours.value),
    max_new_message_age_hours: Number(f.max_new_message_age_hours.value),
    max_edit_age_hours: Number(f.max_edit_age_hours.value),
    alert_retention_days: Number(f.alert_retention_days.value),
  };
  try {
    await api("PUT", "/settings", body);
    toast("Ajustes salvos");
    await refreshStatus();
  } catch (e) {
    showError(e);
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
