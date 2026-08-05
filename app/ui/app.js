/* ==========================================================================
   Steam Switcher — UI logic
   No framework. The whole app is one polled state object plus render passes.
   ========================================================================== */
"use strict";

const TOKEN = window.__TOKEN__;
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ── api ──────────────────────────────────────────────────────────────── */
async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Token": TOKEN, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ── helpers ──────────────────────────────────────────────────────────── */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;

function relTime(unix) {
  if (!unix) return "never";
  const secs = Date.now() / 1000 - unix;
  if (secs < 0) return "just now";
  const steps = [[60, "s"], [3600, "m"], [86400, "h"], [604800, "d"], [2629800, "w"], [31557600, "mo"]];
  if (secs < 60) return "just now";
  for (let i = 1; i < steps.length; i++) {
    if (secs < steps[i][0]) return `${Math.floor(secs / steps[i - 1][0])}${steps[i - 1][1]} ago`;
  }
  return `${Math.floor(secs / 31557600)}y ago`;
}

function hours(minutes) {
  if (!minutes) return "0h";
  if (minutes < 60) return `${minutes}m`;
  const h = minutes / 60;
  return h < 100 ? `${h.toFixed(1)}h` : `${Math.round(h).toLocaleString()}h`;
}

/* Stable hue per account so the generated avatar never changes between runs. */
function hueOf(id) {
  let hash = 0;
  for (const ch of String(id)) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash % 360;
}

const initials = (name) => String(name || "?").trim().split(/\s+/).slice(0, 2)
  .map((w) => w[0]).join("").toUpperCase() || "?";

function avatarHtml(acc, cls = "av") {
  const style = `--h:${hueOf(acc.steam64)}`;
  return acc.has_avatar
    ? `<div class="${cls}" style="${style}"><img src="/avatar/${acc.steam64}.png?t=${TOKEN}" alt=""></div>`
    : `<div class="${cls}" style="${style}">${esc(initials(acc.persona_name))}</div>`;
}

function toast(message, kind = "ok") {
  const name = kind === "err" ? "alert" : kind === "warn" ? "alert" : "check";
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `${icon(name)}<span>${esc(message)}</span>`;
  $("#toasts").append(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, kind === "err" ? 6000 : 3200);
}

/* ── state ────────────────────────────────────────────────────────────── */
const S = {
  data: null,
  filter: "",
  view: "accounts",
  drawerId: null,
  switching: false,
  guardTimer: null,
  pollTimer: null,
  games: {},          // steam64 -> rendered library HTML, so reopening is instant
};

const visibleAccounts = () => {
  if (!S.data) return [];
  const q = S.filter.trim().toLowerCase();
  if (!q) return S.data.accounts;
  return S.data.accounts.filter((a) =>
    [a.persona_name, a.account_name, a.steam64, a.meta.label, a.meta.note]
      .some((f) => String(f || "").toLowerCase().includes(q)));
};

const accountById = (id) => S.data?.accounts.find((a) => a.steam64 === id) || null;

/* ── refresh loop ─────────────────────────────────────────────────────── */
async function refresh() {
  try {
    S.data = await api("/api/state");
    renderAll();
  } catch (err) {
    $("#now-name").textContent = "Connection lost";
    $("#now-sub").textContent = err.message;
  }
}

function startPolling() {
  clearInterval(S.pollTimer);
  S.pollTimer = setInterval(() => { if (!S.switching) refresh(); }, 2500);
}

/* ── render: shell ────────────────────────────────────────────────────── */
function renderAll() {
  const d = S.data;
  if (!d) return;

  document.documentElement.dataset.accent = d.settings.accent;
  applyTheme(d.settings.theme);
  document.body.classList.toggle("compact", !!d.settings.compact_cards);
  $("#rail-version").textContent = `v${d.version}`;

  renderStatus();
  renderBanners();
  renderGrid();
  renderSettings();
  // The drawer is deliberately NOT re-rendered here. It holds live text the
  // user may be typing (label, note, a password) plus the loaded game list and
  // the ticking Guard code; rebuilding it every poll would drop the caret and
  // wipe all of it. It re-renders only when opened or after an explicit edit.
}

function applyTheme(theme) {
  const dark = theme === "auto"
    ? matchMedia("(prefers-color-scheme: dark)").matches
    : theme !== "light";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function renderStatus() {
  const { steam, accounts } = S.data;
  const dot = $("#now-dot");
  dot.className = "now-dot";

  if (!S.data.steam_found) {
    $("#now-name").textContent = "Steam not found";
    $("#now-sub").textContent = S.data.steam_error;
    return;
  }
  if (!steam.running) {
    $("#now-name").textContent = "Steam is not running";
    $("#now-sub").textContent = `${accounts.length} account${accounts.length === 1 ? "" : "s"} ready`;
    return;
  }

  const active = accounts.find((a) => a.is_active);
  dot.classList.add(steam.running_app_id ? "busy" : "on");
  $("#now-name").textContent = active ? active.persona_name : "Signed out";
  $("#now-sub").textContent = active
    ? (steam.running_app_id ? `in-game · ${active.account_name}` : `signed in · ${active.account_name}`)
    : "Steam is on the login screen";
}

/* ── render: banners ──────────────────────────────────────────────────── */
function renderBanners() {
  const host = $("#banners");
  host.innerHTML = "";

  if (!S.data.steam_found) {
    host.insertAdjacentHTML("beforeend", banner("danger", "alert", "Steam could not be found",
      esc(S.data.steam_error), `<button class="btn btn-sm" data-go="settings">Set the folder</button>`));
    return;
  }

  for (const check of S.data.preflight) {
    host.insertAdjacentHTML("beforeend",
      banner(check.level, "alert", esc(check.title), esc(check.detail)));
  }
}

function banner(level, ic, title, body, actions = "") {
  return `<div class="banner is-${level}">${icon(ic)}
    <div class="banner-body"><b>${title}</b><span>${body}</span></div>
    ${actions ? `<div class="banner-actions">${actions}</div>` : ""}</div>`;
}

/* ── render: account grid ─────────────────────────────────────────────── */
function renderGrid() {
  const grid = $("#grid");
  const list = visibleAccounts();
  $("#empty").hidden = list.length > 0 || !S.data.steam_found;

  grid.innerHTML = list.map((acc, i) => {
    const m = acc.meta;
    const tint = m.color ? `--tint:${esc(m.color)}` : "";
    const pills = [];
    if (acc.is_active) pills.push(`<span class="pill pill-live">${icon("check")}Signed in</span>`);
    // Describes what the *next* switch to this account will cost, which is why
    // it still shows on the account that happens to be signed in right now.
    pills.push(acc.has_token
      ? `<span class="pill pill-ok" title="Steam holds a refresh token — switching signs in with no password">Instant</span>`
      : `<span class="pill pill-warn" title="No stored token yet — Steam will ask for this password once">Needs password</span>`);
    if (m.label) pills.push(`<span class="pill pill-label" style="${tint}">${esc(m.label)}</span>`);
    if (acc.vault.shared_secret) pills.push(`<span class="pill">${icon("shield")}2FA</span>`);

    return `
    <article class="card ${acc.is_active ? "is-active" : ""}" data-id="${acc.steam64}" draggable="true"
             style="${tint};animation-delay:${Math.min(i * 26, 260)}ms">
      ${i < 9 ? `<span class="kbd-hint">${i + 1}</span>` : ""}
      <div class="card-top">
        ${avatarHtml(acc)}
        <div class="card-id">
          <b>${esc(acc.persona_name)}</b>
          <span>${esc(acc.account_name)}</span>
        </div>
        <button class="card-star ${m.favorite ? "on" : ""}" data-star="${acc.steam64}"
                title="${m.favorite ? "Remove from favourites" : "Add to favourites"}"
                aria-label="Toggle favourite">${icon("star")}</button>
      </div>

      <div class="pills">${pills.join("")}</div>

      <div class="card-meta">
        <span>Last used <b>${relTime(acc.last_login)}</b></span>
        ${m.switches ? `<span>·</span><span><b>${m.switches}</b> switch${m.switches === 1 ? "" : "es"}</span>` : ""}
      </div>

      ${m.note ? `<div class="card-note" style="${tint}">${esc(m.note)}</div>` : ""}

      <div class="card-actions">
        <button class="btn" data-switch="${acc.steam64}" ${acc.is_active ? "disabled" : ""}>
          ${acc.is_active ? "Current account" : "Switch"}
        </button>
        <button class="btn btn-ghost" data-open="${acc.steam64}" title="Details">${icon("cog")}</button>
      </div>
    </article>`;
  }).join("");
}

/* ── drag to reorder ──────────────────────────────────────────────────── */
/* Dragging a card is what gives the "Custom" sort order something to sort by,
   so the first drag also flips the sort mode - otherwise the new order would
   be saved and then immediately overridden by whatever mode was active. */
let dragId = null;

$("#grid").addEventListener("dragstart", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  dragId = card.dataset.id;
  card.style.opacity = ".4";
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragId);
});

$("#grid").addEventListener("dragover", (e) => {
  if (!dragId) return;
  e.preventDefault();
  const over = e.target.closest(".card");
  const dragged = $(`.card[data-id="${dragId}"]`);
  if (!over || !dragged || over === dragged) return;

  // Insert before or after depending on which half of the card we are over.
  const box = over.getBoundingClientRect();
  const after = e.clientX > box.left + box.width / 2;
  over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
});

$("#grid").addEventListener("dragend", async () => {
  const dragged = $(`.card[data-id="${dragId}"]`);
  if (dragged) dragged.style.opacity = "";
  if (!dragId) return;
  dragId = null;

  const order = $$("#grid .card").map((c) => c.dataset.id);
  try {
    await api("/api/reorder", { order });
    if (S.data.settings.sort_mode !== "favorite") {
      await saveSetting("sort_mode", "favorite");
      toast("Custom order saved — sorting switched to Custom");
    } else {
      await refresh();
    }
  } catch (err) { toast(err.message, "err"); }
});

/* ── render: settings ─────────────────────────────────────────────────── */
function renderSettings() {
  const s = S.data.settings;
  $$("[data-setting]").forEach((el) => {
    const key = el.dataset.setting;
    if (el.type === "checkbox") el.checked = !!s[key];
    else if (document.activeElement !== el) el.value = s[key];
  });
  syncSeg("#set-theme", s.theme);
  syncSeg("#set-sort", s.sort_mode);
  $$("#set-accent button").forEach((b) => b.classList.toggle("on", b.dataset.value === s.accent));

  if (document.activeElement !== $("#set-steampath")) $("#set-steampath").value = s.steam_path_override || "";
  if (S.data.steam_found) {
    $("#steam-root-now").textContent = S.data.steam.root;
    $("#libs-now").textContent = S.data.steam.libraries.join("  ·  ");
  }
}

const syncSeg = (sel, value) =>
  $$(`${sel} button`).forEach((b) => b.classList.toggle("on", b.dataset.value === value));

/* ── switching ────────────────────────────────────────────────────────── */
async function requestSwitch(steam64, opts = {}) {
  const acc = accountById(steam64);
  if (!acc || acc.is_active) return;

  const risks = S.data.preflight;
  const needsConfirm = risks.length > 0 || S.data.settings.confirm_before_switch;
  if (needsConfirm && !opts.force) {
    const go = await confirmSwitch(acc, risks);
    if (!go) return;
  }
  await beginSwitch(steam64, opts.app_id);
}

function confirmSwitch(acc, risks) {
  return new Promise((resolve) => {
    const checks = risks.map((r) => `
      <div class="issue lv-${r.level}">${icon("alert")}
        <div class="issue-body"><b>${esc(r.title)}</b><span>${esc(r.detail)}</span></div>
      </div>`).join("");

    $("#dialog").innerHTML = `
      <h3>Switch to ${esc(acc.persona_name)}?</h3>
      <p>Steam will close and restart as <code>${esc(acc.account_name)}</code>.
      ${acc.has_token
        ? "It will sign in automatically — no password needed."
        : "This account has no saved token yet, so Steam will ask for its password once."}</p>
      ${checks ? `<div class="dialog-checks">${checks}</div>` : ""}
      <div class="dialog-foot">
        <button class="btn btn-ghost" data-act="cancel">Cancel</button>
        <button class="btn ${risks.some((r) => r.level === "danger") ? "btn-warn" : ""}" data-act="ok">
          ${risks.length ? "Switch anyway" : "Switch"}
        </button>
      </div>`;
    $("#modal").hidden = false;

    const done = (value) => {
      $("#modal").hidden = true;
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (e) => { if (e.key === "Escape") done(false); };
    $("#dialog").onclick = (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act) done(act === "ok");
    };
    document.addEventListener("keydown", onKey);
    setTimeout(() => $('#dialog [data-act="ok"]')?.focus(), 30);
  });
}

async function beginSwitch(steam64, appId) {
  const acc = accountById(steam64);
  S.switching = true;
  closeDrawer();

  $("#sw-avatar").outerHTML = avatarHtml(acc, "av av-lg").replace("<div ", '<div id="sw-avatar" ');
  $("#sw-name").textContent = acc.persona_name;
  $("#sw-sub").textContent = `switching to ${acc.account_name}`;
  $("#sw-steps").innerHTML = "";
  $("#sw-close").hidden = true;
  $("#sw-spinner").hidden = false;
  $("#overlay").hidden = false;
  $(`.card[data-id="${steam64}"]`)?.classList.add("is-switching");

  let job;
  try {
    job = await api("/api/switch", { steam64, app_id: appId });
  } catch (err) {
    renderSteps([{ message: err.message, status: "error" }]);
    finishSwitch(false);
    return;
  }

  const poll = setInterval(async () => {
    let snap;
    try {
      snap = await api(`/api/switch/${job.id}`);
    } catch { return; }

    renderSteps(snap.steps);
    $("#sw-elapsed").textContent = `${snap.elapsed.toFixed(1)}s`;
    if (snap.state === "done" || snap.state === "error") {
      clearInterval(poll);
      finishSwitch(snap.state === "done");
    }
  }, 350);
}

function renderSteps(steps) {
  $("#sw-steps").innerHTML = steps.map((s) => {
    const ic = s.status === "error" ? "x" : s.status === "warn" ? "alert" : "check";
    return `<li class="step st-${s.status}">
      <span class="step-icon">${icon(ic)}</span><span>${esc(s.message)}</span></li>`;
  }).join("");
  const list = $("#sw-steps");
  list.scrollTop = list.scrollHeight;
}

function finishSwitch(ok) {
  $("#sw-spinner").hidden = true;
  $("#sw-close").hidden = false;
  $("#sw-sub").textContent = ok ? "done" : "failed";
  $("#sw-close").focus();
  S.switching = false;
  refresh();

  if (ok && S.data?.settings.close_after_switch) {
    setTimeout(() => api("/api/quit").catch(() => {}), 1200);
  }
}

/* ── drawer ───────────────────────────────────────────────────────────── */
async function openDrawer(steam64) {
  S.drawerId = steam64;
  $("#drawer").hidden = false;
  renderDrawer();
  loadGames(steam64);
  startGuard(steam64);
}

function closeDrawer() {
  S.drawerId = null;
  $("#drawer").hidden = true;
  clearInterval(S.guardTimer);
}

function renderDrawer() {
  const acc = accountById(S.drawerId);
  if (!acc) return closeDrawer();
  const m = acc.meta;
  const vaultOn = S.data.settings.enable_vault && S.data.dpapi;
  const guardOn = S.data.settings.enable_guard && S.data.dpapi;

  $("#drawer-inner").innerHTML = `
    <div class="drawer-head">
      ${avatarHtml(acc, "av av-lg")}
      <div class="card-id">
        <b>${esc(acc.persona_name)}</b>
        <span>${esc(acc.account_name)}</span>
      </div>
      <button class="drawer-close" data-act="close" aria-label="Close">${icon("x")}</button>
    </div>

    <div class="card-actions">
      <button class="btn" data-act="switch" ${acc.is_active ? "disabled" : ""}>
        ${acc.is_active ? "Current account" : "Switch to this account"}
      </button>
      <button class="btn btn-ghost" data-act="profile" title="Open Steam profile">${icon("play")}</button>
    </div>

    <div class="dsection">
      <h3>Identity</h3>
      ${idRow("SteamID64", acc.ids.steam64)}
      ${idRow("SteamID", acc.ids.steam2)}
      ${idRow("SteamID3", acc.ids.steam3)}
      ${idRow("Account ID", acc.ids.account_id)}
      ${idRow("Login token", acc.has_token ? "stored — switches instantly" : "none — one sign-in needed")}
      ${idRow("Last sign-in", relTime(acc.last_login))}
    </div>

    <div class="dsection">
      <h3>Organise</h3>
      <div class="field-row">
        <div class="field"><label>Label</label>
          <input id="d-label" value="${esc(m.label)}" placeholder="Main, Smurf, Trading…" maxlength="24"></div>
        <div class="field" style="flex:0 0 74px"><label>Colour</label>
          <input id="d-color" type="color" value="${esc(m.color || "#66c0f4")}" style="padding:3px;height:37px"></div>
      </div>
      <div class="field"><label>Note</label>
        <textarea id="d-note" placeholder="Anything worth remembering about this account…" maxlength="400">${esc(m.note)}</textarea></div>
    </div>

    ${guardOn ? `
    <div class="dsection">
      <h3>Steam Guard</h3>
      <div id="guard-box">
        ${acc.vault.shared_secret
          ? `<div class="guard">
               <svg class="guard-ring" viewBox="0 0 34 34">
                 <circle class="bg" cx="17" cy="17" r="14"/>
                 <circle class="fg" id="guard-arc" cx="17" cy="17" r="14" stroke-dasharray="87.96" stroke-dashoffset="0"/>
               </svg>
               <div><div class="guard-code" id="guard-code">·····</div>
                 <span style="font-size:11.5px;color:var(--ink-3)" id="guard-left">syncing…</span></div>
               <button class="btn btn-ghost btn-sm" data-act="guard-copy" style="margin-left:auto">${icon("copy")}</button>
             </div>
             <button class="btn btn-ghost btn-sm" data-act="guard-remove">Remove shared secret</button>`
          : `<div class="field"><label>Shared secret from your <code>.maFile</code> (Steam Desktop Authenticator)</label>
               <input id="d-secret" placeholder="base64 shared_secret" spellcheck="false"></div>
             <button class="btn btn-soft btn-sm" data-act="guard-save">Store secret</button>`}
      </div>
    </div>` : ""}

    ${vaultOn && !acc.has_token ? `
    <div class="dsection">
      <h3>Password</h3>
      <p style="font-size:12px;color:var(--ink-3);line-height:1.55">
        Sealed with Windows DPAPI and only ever placed on the clipboard — never passed
        to Steam on the command line, where any process could read it.</p>
      ${acc.vault.password
        ? `<div class="field-row">
             <button class="btn btn-soft btn-sm" data-act="pw-copy" style="flex:1">${icon("copy")} Copy password</button>
             <button class="btn btn-ghost btn-sm" data-act="pw-remove">Remove</button>
           </div>`
        : `<div class="field"><input id="d-pw" type="password" placeholder="Password for this account" spellcheck="false"></div>
           <button class="btn btn-soft btn-sm" data-act="pw-save">Store password</button>`}
    </div>` : ""}

    <div class="dsection">
      <h3>Library</h3>
      <div class="games" id="d-games">${S.games[acc.steam64]
        || `<div class="panel-note">Reading Steam config…</div>`}</div>
    </div>

    <div class="dsection">
      <h3>Danger zone</h3>
      <button class="btn btn-ghost btn-sm btn-danger" data-act="forget">
        Forget this account's Switcher data
      </button>
      <span style="font-size:11.5px;color:var(--ink-3)">
        Clears the label, note and any stored secrets. The Steam account itself is untouched.</span>
    </div>`;
}

const idRow = (label, value) =>
  `<div class="idrow" data-copy="${esc(value)}"><span>${esc(label)}</span><code>${esc(value)}</code>${icon("copy")}</div>`;

async function loadGames(steam64) {
  try {
    const data = await api(`/api/games/${steam64}?limit=10`);

    let html;
    if (!data.games.length) {
      html = `<div class="panel-note">No game activity recorded on this PC for this account.</div>`;
    } else {
      const plural = data.suspect_count === 1 ? "entry" : "entries";
      html = `<div class="panel-note" style="margin:0 0 6px">
          <b style="color:var(--ink-2)">${data.total}</b> games ·
          <b style="color:var(--ink-2)">${data.installed_count}</b> installed ·
          <b style="color:var(--ink-2)">${hours(data.total_minutes)}</b> local playtime
          ${data.suspect_count ? `<br><span style="color:var(--warn)">${data.suspect_count} ${plural} with an implausible counter excluded from the total</span>` : ""}
        </div>` + data.games.map((g) => `
        <div class="game">
          <span class="game-name" title="${esc(g.name)}">${esc(g.name)}</span>
          ${g.suspect ? `<span class="game-suspect" title="Steam's own local counter for this game is corrupt">${icon("alert")}</span>` : ""}
          <span class="game-time">${g.minutes ? hours(g.minutes) : relTime(g.last_played)}</span>
          ${g.installed ? `<button class="game-play" data-launch="${g.appid}"
              title="Switch to this account and launch ${esc(g.name)}">${icon("play")}</button>` : ""}
        </div>`).join("");
    }

    S.games[steam64] = html;
    if (S.drawerId === steam64 && $("#d-games")) $("#d-games").innerHTML = html;
  } catch { /* drawer closed or Steam moved; nothing useful to show */ }
}

function startGuard(steam64) {
  clearInterval(S.guardTimer);
  const acc = accountById(steam64);
  if (!acc?.vault.shared_secret || !S.data.settings.enable_guard) return;

  const tick = async () => {
    if (S.drawerId !== steam64) return clearInterval(S.guardTimer);
    try {
      const g = await api(`/api/guard/${steam64}`);
      if (!g.has_secret) return;
      const code = $("#guard-code");
      if (!code) return;
      code.textContent = g.code;
      $("#guard-left").textContent = `expires in ${g.remaining}s`;
      const circumference = 2 * Math.PI * 14;
      $("#guard-arc").style.strokeDashoffset = circumference * (1 - g.remaining / g.period);
    } catch { /* transient */ }
  };
  tick();
  S.guardTimer = setInterval(tick, 1000);
}

/* ── history / doctor views ───────────────────────────────────────────── */
async function loadHistory() {
  const { history, stats } = await api("/api/history");
  $("#stat-row").innerHTML = [
    [stats.total_switches, "switches"],
    [stats.avg_seconds ? `${stats.avg_seconds}s` : "—", "average time"],
    [stats.fastest_seconds ? `${stats.fastest_seconds}s` : "—", "fastest"],
    [stats.most_used || "—", "most used"],
  ].map(([v, l]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");

  $("#timeline").innerHTML = history.length
    ? history.slice(0, 40).map((h) => `
      <div class="tl">
        <span class="tl-dot ${h.ok ? "" : "bad"}"></span>
        <div class="tl-body">
          <b>${esc(h.persona || h.account_name || h.steam64)}</b>
          <span>${h.ok ? `switched in ${h.duration}s` : esc(h.error || "failed")}</span>
        </div>
        <span class="tl-time">${relTime(h.at)}</span>
      </div>`).join("")
    : `<div class="panel-note">No switches recorded yet.</div>`;

  const { backups } = await api("/api/backups");
  $("#backups").innerHTML = backups.length
    ? backups.map((b) => `
      <div class="bk">
        <div class="bk-body">
          <b>${esc(b.name)}</b>
          <span>auto-login was ${esc(b.auto_login_user || "unset")} · ${relTime(b.created)}</span>
        </div>
        <button class="btn btn-ghost btn-sm" data-restore="${esc(b.name)}">Restore</button>
      </div>`).join("")
    : `<div class="panel-note">No restore points yet.</div>`;
}

async function loadDoctor() {
  const { issues } = await api("/api/health");
  const badge = $("#doctor-badge");
  badge.hidden = !issues.some((i) => i.level === "danger" || i.level === "warn");

  // An info notice must not get the tick icon - that reads as "all clear".
  const issueIcon = { danger: "alert", warn: "alert", info: "shield" };

  $("#issues").innerHTML = issues.length
    ? issues.map((i) => `
      <div class="issue lv-${i.level}">${icon(issueIcon[i.level] || "check")}
        <div class="issue-body"><b>${esc(i.title)}</b><span>${esc(i.detail)}</span></div>
        ${i.fixable ? `<button class="btn btn-sm btn-soft" data-fix="${esc(i.code)}">Fix</button>` : ""}
      </div>`).join("")
    : `<div class="issue lv-ok">${icon("check")}
         <div class="issue-body"><b>Everything looks healthy</b>
         <span>Steam is where it should be and every account is ready to switch.</span></div></div>`;
}

async function loadLegacy() {
  try {
    const data = await api("/api/legacy/scan");
    const note = $("#legacy-note");
    if (!data.found) {
      note.textContent = "No legacy Steam.bat was found next to this app.";
      $("#legacy-list").innerHTML = "";
      return;
    }
    const risky = data.accounts.filter((a) => a.has_password).length;
    note.innerHTML = `Found <code>${esc(data.path.split("\\").pop())}</code> with
      ${data.accounts.length} account(s).
      ${risky ? `<b style="color:var(--warn)">${risky} plaintext password(s)</b> are sitting in that file —
      anyone with a copy of that file can read them.
      Rotate them at <code>store.steampowered.com/account</code> rather than reusing them.` : ""}`;

    $("#legacy-list").innerHTML = data.accounts.map((a) => `
      <div class="lg">
        <div class="lg-body">
          <b>${esc(a.username)}</b>
          <span>${a.matched
            ? `matches ${esc(a.persona_name)}`
            : "not signed in on this PC yet"}${a.password_was_broken
            ? " · this password contained <code>&amp;</code> or <code>^</code>, which cmd.exe mangled — the old script could never log in with it"
            : ""}</span>
        </div>
        ${a.matched ? `<span class="pill pill-ok">linked</span>` : `<span class="pill">unknown</span>`}
      </div>`).join("");
  } catch { /* Steam not found; the settings panel already says so */ }
}

/* ── command palette ──────────────────────────────────────────────────── */
const PALETTE_ACTIONS = [
  { id: "view:accounts", label: "Go to Accounts", icon: "users", run: () => setView("accounts") },
  { id: "view:history", label: "Go to History", icon: "clock", run: () => setView("history") },
  { id: "view:doctor", label: "Run health check", icon: "stethoscope", run: () => setView("doctor") },
  { id: "view:settings", label: "Go to Settings", icon: "cog", run: () => setView("settings") },
  { id: "act:refresh", label: "Refresh Steam state", icon: "refresh", run: refresh },
  { id: "act:backup", label: "Create a restore point", icon: "shield",
    run: async () => { const r = await api("/api/backup", {}); toast(`Restore point ${r.name} created`); } },
  { id: "act:shutdown", label: "Close Steam gracefully", icon: "power",
    run: async () => { toast("Asking Steam to close…"); const r = await api("/api/steam/shutdown", {});
      toast(r.forced ? "Steam had to be forced closed" : "Steam closed cleanly", r.forced ? "warn" : "ok"); refresh(); } },
  { id: "act:launch", label: "Start Steam", icon: "play",
    run: async () => { const r = await api("/api/steam/launch", {}); toast(r.message); } },
  { id: "act:theme", label: "Toggle light / dark theme", icon: "cog",
    run: () => saveSetting("theme", S.data.settings.theme === "light" ? "dark" : "light") },
  { id: "act:quit", label: "Quit Steam Switcher", icon: "x", run: () => api("/api/quit") },
];

let paletteItems = [];
let paletteIndex = 0;

function openPalette() {
  $("#palette").hidden = false;
  $("#palette-input").value = "";
  $("#palette-input").focus();
  renderPalette("");
}

const closePalette = () => { $("#palette").hidden = true; };

function renderPalette(query) {
  const q = query.trim().toLowerCase();
  const accounts = (S.data?.accounts || []).map((a) => ({
    id: `sw:${a.steam64}`,
    label: `Switch to ${a.persona_name}`,
    hint: a.account_name,
    account: a,
    run: () => requestSwitch(a.steam64),
  }));

  const all = [...accounts, ...PALETTE_ACTIONS];
  paletteItems = q
    ? all.filter((i) => (i.label + " " + (i.hint || "")).toLowerCase().includes(q))
    : all;
  paletteIndex = 0;

  $("#palette-list").innerHTML = paletteItems.map((item, i) => `
    <div class="pitem ${i === 0 ? "on" : ""}" data-i="${i}">
      ${item.account
        ? avatarHtml(item.account, "av av-sm")
        : `<span class="pitem-icon">${icon(item.icon)}</span>`}
      <b>${esc(item.label)}</b>
      ${item.hint ? `<small>${esc(item.hint)}</small>` : ""}
    </div>`).join("") || `<div class="panel-note" style="padding:14px">Nothing matches “${esc(query)}”.</div>`;
}

function movePalette(delta) {
  if (!paletteItems.length) return;
  paletteIndex = (paletteIndex + delta + paletteItems.length) % paletteItems.length;
  $$("#palette-list .pitem").forEach((el, i) => el.classList.toggle("on", i === paletteIndex));
  $(`#palette-list .pitem.on`)?.scrollIntoView({ block: "nearest" });
}

function runPalette(index) {
  const item = paletteItems[index];
  if (!item) return;
  closePalette();
  Promise.resolve(item.run()).catch((e) => toast(e.message, "err"));
}

/* ── settings persistence ─────────────────────────────────────────────── */
async function saveSetting(key, value) {
  try {
    const r = await api("/api/settings", { [key]: value });
    S.data.settings = r.settings;
    if (r.message) toast(r.message, "err");
    renderAll();
  } catch (e) { toast(e.message, "err"); }
}

const VIEWS = ["accounts", "history", "doctor", "settings"];

function setView(name) {
  if (!VIEWS.includes(name)) name = "accounts";
  S.view = name;
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${name}`));
  if (name === "history") loadHistory().catch(() => {});
  if (name === "doctor") loadDoctor().catch(() => {});
  if (name === "settings") loadLegacy();
}

/* Deep links: #settings opens a view, #account/<steamid> opens that drawer.
   Handy for pinning a shortcut straight to the panel you want. */
function applyHash() {
  const hash = decodeURIComponent(location.hash.slice(1));
  const [head, arg] = hash.split("/");
  if (head === "account" && arg && accountById(arg)) {
    setView("accounts");
    openDrawer(arg);
  } else if (VIEWS.includes(head)) {
    setView(head);
  }
}

/* ── events ───────────────────────────────────────────────────────────── */
document.addEventListener("click", async (e) => {
  const t = e.target;
  const hit = (attr) => t.closest(`[${attr}]`)?.getAttribute(attr);

  const nav = t.closest(".nav-item");
  if (nav) return setView(nav.dataset.view);

  const go = hit("data-go");
  if (go) return setView(go);

  const star = hit("data-star");
  if (star) {
    const acc = accountById(star);
    await api(`/api/meta/${star}`, { favorite: !acc.meta.favorite });
    return refresh();
  }

  const sw = hit("data-switch");
  if (sw) return requestSwitch(sw);

  const open = hit("data-open");
  if (open) return openDrawer(open);

  const copy = hit("data-copy");
  if (copy) {
    await api("/api/clipboard", { text: copy });
    return toast("Copied to clipboard");
  }

  const launch = hit("data-launch");
  if (launch) {
    // No switch needed when this account is already signed in - just run it.
    if (accountById(S.drawerId)?.is_active) {
      await api("/api/open", { target: `steam://rungameid/${launch}` });
      return toast("Launching…");
    }
    return requestSwitch(S.drawerId, { app_id: launch });
  }

  const restore = hit("data-restore");
  if (restore) {
    try {
      const r = await api("/api/restore", { name: restore });
      toast(r.message, r.ok ? "ok" : "err");
      loadHistory();
    } catch (err) { toast(err.message, "err"); }
    return;
  }

  const fix = hit("data-fix");
  if (fix) {
    try {
      const r = await api("/api/fix", { code: fix });
      toast(r.message, r.ok ? "ok" : "err");
      loadDoctor(); refresh();
    } catch (err) { toast(err.message, "err"); }
    return;
  }

  const pitem = t.closest(".pitem");
  if (pitem) return runPalette(Number(pitem.dataset.i));

  if (t.closest("#btn-palette")) return openPalette();
  if (t.closest("#btn-refresh")) { await refresh(); return toast("Refreshed"); }
  if (t.closest("#btn-quit")) return api("/api/quit").catch(() => {});
  if (t.closest("#sw-close")) { $("#overlay").hidden = true; return; }
  if (t.closest("#btn-recheck")) return loadDoctor();
  if (t.closest("#btn-clear-history")) { await api("/api/history/clear", {}); return loadHistory(); }
  if (t.closest("#btn-backup")) {
    const r = await api("/api/backup", {});
    toast(`Restore point ${r.name} created`);
    return loadHistory();
  }
  if (t.closest("#btn-steam-folder")) {
    return api("/api/open", { kind: "folder", target: S.data.steam.root }).catch((err) => toast(err.message, "err"));
  }
  if (t.closest("#btn-savepath")) {
    return saveSetting("steam_path_override", $("#set-steampath").value.trim());
  }

  const seg = t.closest("#set-theme button, #set-sort button, #set-accent button");
  if (seg) {
    const key = seg.closest("#set-theme") ? "theme" : seg.closest("#set-sort") ? "sort_mode" : "accent";
    return saveSetting(key, seg.dataset.value);
  }

  if (t.closest("#palette") && !t.closest(".palette")) return closePalette();

  await handleDrawerAction(t);
});

async function handleDrawerAction(target) {
  const act = target.closest("[data-act]")?.dataset.act;
  if (!act || !S.drawerId) return;
  const id = S.drawerId;

  try {
    switch (act) {
      case "close": return closeDrawer();
      case "switch": return requestSwitch(id);
      case "profile":
        return api("/api/open", { target: accountById(id).ids.profile_url });
      case "guard-save": {
        const value = $("#d-secret").value.trim();
        if (!value) return;
        await api("/api/vault", { steam64: id, kind: "shared_secret", value });
        toast("Shared secret sealed with DPAPI");
        await refresh(); openDrawer(id);
        return;
      }
      case "guard-remove":
        await api("/api/vault", { steam64: id, kind: "shared_secret", value: "" });
        toast("Shared secret removed");
        await refresh(); openDrawer(id);
        return;
      case "guard-copy": {
        const r = await api("/api/vault/copy", { steam64: id, kind: "shared_secret" });
        return toast(`Guard code copied — clipboard clears in ${r.wipes_in}s`);
      }
      case "pw-save": {
        const value = $("#d-pw").value;
        if (!value) return;
        await api("/api/vault", { steam64: id, kind: "password", value });
        toast("Password sealed with DPAPI");
        await refresh(); openDrawer(id);
        return;
      }
      case "pw-remove":
        await api("/api/vault", { steam64: id, kind: "password", value: "" });
        toast("Password removed");
        await refresh(); openDrawer(id);
        return;
      case "pw-copy": {
        const r = await api("/api/vault/copy", { steam64: id, kind: "password" });
        return toast(`Password copied — clipboard clears in ${r.wipes_in}s`);
      }
      case "forget":
        await api("/api/forget", { steam64: id });
        toast("Switcher data cleared for this account");
        closeDrawer();
        return refresh();
    }
  } catch (err) { toast(err.message, "err"); }
}

/* Debounced writes so typing a note does not hit the API on every keystroke. */
let metaTimer;
document.addEventListener("input", (e) => {
  const t = e.target;

  if (t.id === "search") { S.filter = t.value; return renderGrid(); }

  if (["d-label", "d-note", "d-color"].includes(t.id) && S.drawerId) {
    clearTimeout(metaTimer);
    metaTimer = setTimeout(async () => {
      await api(`/api/meta/${S.drawerId}`, {
        label: $("#d-label").value.trim(),
        note: $("#d-note").value.trim(),
        color: $("#d-color").value,
      });
      const keep = S.drawerId;
      await refresh();
      S.drawerId = keep;
    }, 450);
    return;
  }

  if (t.id === "palette-input") return renderPalette(t.value);

  const key = t.dataset.setting;
  if (key) {
    const value = t.type === "checkbox" ? t.checked
      : t.type === "number" ? Number(t.value) : t.value;
    clearTimeout(metaTimer);
    metaTimer = setTimeout(() => saveSetting(key, value), t.type === "number" ? 500 : 0);
  }
});

/* ── keyboard ─────────────────────────────────────────────────────────── */
document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName);
  const paletteOpen = !$("#palette").hidden;

  if (e.key === "Alt") document.body.classList.add("show-hints");

  if (paletteOpen) {
    if (e.key === "Escape") { e.preventDefault(); return closePalette(); }
    if (e.key === "ArrowDown") { e.preventDefault(); return movePalette(1); }
    if (e.key === "ArrowUp") { e.preventDefault(); return movePalette(-1); }
    if (e.key === "Enter") { e.preventDefault(); return runPalette(paletteIndex); }
    return;
  }

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); return openPalette(); }

  if (e.key === "Escape") {
    if (!$("#overlay").hidden && !$("#sw-close").hidden) return ($("#overlay").hidden = true);
    if (!$("#drawer").hidden) return closeDrawer();
    if (typing) return document.activeElement.blur();
  }

  if (typing) return;

  if (e.key === "/") { e.preventDefault(); return $("#search").focus(); }
  if (e.key.toLowerCase() === "r") return refresh();
  if (e.key >= "1" && e.key <= "9") {
    const acc = visibleAccounts()[Number(e.key) - 1];
    if (acc && !acc.is_active) requestSwitch(acc.steam64);
  }
});

document.addEventListener("keyup", (e) => {
  if (e.key === "Alt") document.body.classList.remove("show-hints");
});

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (S.data?.settings.theme === "auto") applyTheme("auto");
});

/* ── boot ─────────────────────────────────────────────────────────────── */
window.addEventListener("hashchange", applyHash);

(async function boot() {
  await refresh();
  startPolling();
  loadDoctor().catch(() => {});
  loadLegacy();
  applyHash();
})();
