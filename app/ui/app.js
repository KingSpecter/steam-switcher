// Steam Switcher UI. No framework: one polled state object and a few render passes.
"use strict";

const TOKEN = document.querySelector('meta[name="ss-token"]').content;
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---- API ----
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

// ---- Helpers ----
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

function relTime(unix) {
  if (!unix) return "never";
  const secs = Date.now() / 1000 - unix;
  if (secs < 60) return "just now";
  const steps = [[60, "m"], [3600, "h"], [86400, "d"], [604800, "w"], [2629800, "mo"], [31557600, "y"]];
  let unit = steps[0];
  for (const step of steps) if (secs >= step[0]) unit = step;
  return `${Math.floor(secs / unit[0])}${unit[1]} ago`;
}

function hours(minutes) {
  if (!minutes) return "0h";
  if (minutes < 60) return `${minutes}m`;
  const h = minutes / 60;
  return h < 100 ? `${h.toFixed(1)}h` : `${Math.round(h).toLocaleString()}h`;
}

// stable hue per account, so the fallback avatar is the same every run
function hueOf(id) {
  let hash = 0;
  for (const ch of String(id)) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash % 360;
}

const initials = (name) => String(name || "?").trim().split(/\s+/).slice(0, 2)
  .map((w) => w[0]).join("").toUpperCase() || "?";

function avatarHtml(acc, cls = "av") {
  return acc.has_avatar
    ? `<div class="${cls}" data-hue="${hueOf(acc.steam64)}"><img src="/avatar/${acc.steam64}.png?t=${TOKEN}" alt=""></div>`
    : `<div class="${cls}" data-hue="${hueOf(acc.steam64)}">${esc(initials(acc.persona_name))}</div>`;
}

// the page runs under a CSP without inline styles, so colours are set here
function paint(root = document) {
  $$("[data-hue]", root).forEach((el) => el.style.setProperty("--h", el.dataset.hue));
  $$("[data-tint]", root).forEach((el) => {
    if (/^#[0-9a-f]{3,8}$/i.test(el.dataset.tint)) el.style.setProperty("--tint", el.dataset.tint);
  });
}

function toast(message, kind = "ok") {
  const lamp = kind === "err" ? "bad" : kind === "warn" ? "warn" : "on";
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="lamp ${lamp}"></span><span>${esc(message)}</span>`;
  $("#toasts").append(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, kind === "err" ? 6000 : 3200);
}

// ---- State ----
const S = {
  data: null,
  filter: "",
  view: "accounts",
  drawerId: null,
  switching: false,
  guardTimer: null,
  pollTimer: null,
  games: {},          // steam64 -> library html, so reopening the drawer is instant
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

// ---- Refresh loop ----
async function refresh() {
  try {
    S.data = await api("/api/state");
    renderAll();
  } catch (err) {
    setReadout("", "Error", "Connection lost", err.message);
  }
}

function startPolling() {
  clearInterval(S.pollTimer);
  S.pollTimer = setInterval(() => { if (!S.switching) refresh(); }, 2500);
}

// ---- Render: shell ----
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
  // The drawer is not rebuilt on every poll: it holds text being typed, the
  // loaded game list and the ticking Guard code. It renders on open and after edits.
}

function applyTheme(theme) {
  const dark = theme === "auto"
    ? matchMedia("(prefers-color-scheme: dark)").matches
    : theme !== "light";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function setReadout(lamp, tag, name, sub) {
  $("#now-dot").className = `lamp ${lamp}`;
  $("#now-tag").textContent = tag;
  $("#now-name").textContent = name;
  $("#now-sub").textContent = sub;
}

function renderStatus() {
  const { steam, accounts } = S.data;

  if (!S.data.steam_found) {
    return setReadout("", "Status", "Steam not found", S.data.steam_error);
  }
  if (!steam.running) {
    return setReadout("", "Steam off", "Not running", `${plural(accounts.length, "account")} ready`);
  }

  const active = accounts.find((a) => a.is_active);
  if (!active) return setReadout("", "Steam on", "Signed out", "Steam is on the login screen");
  if (steam.running_app_id) {
    return setReadout("busy", "In game", active.persona_name, `${active.account_name} · app ${steam.running_app_id}`);
  }
  setReadout("on", "Signed in", active.persona_name, active.account_name);
}

// ---- Render: banners ----
function renderBanners() {
  const host = $("#banners");
  host.innerHTML = "";

  if (!S.data.steam_found) {
    host.insertAdjacentHTML("beforeend", banner("danger", "Steam could not be found",
      esc(S.data.steam_error), `<button class="key key-sm" data-go="settings">Set the folder</button>`));
    return;
  }

  for (const check of S.data.preflight) {
    host.insertAdjacentHTML("beforeend", banner(check.level, esc(check.title), esc(check.detail)));
  }
}

function banner(level, title, body, actions = "") {
  return `<div class="banner is-${level}">${icon("alert")}
    <div class="banner-body"><b>${title}</b><span>${body}</span></div>
    ${actions ? `<div class="banner-actions">${actions}</div>` : ""}</div>`;
}

// ---- Render: account list ----
function signInState(acc) {
  if (acc.is_active) return `<div class="state on"><span class="lamp on"></span>Signed in</div>`;
  // what the next switch to this account costs
  return acc.has_token
    ? `<div class="state" title="Steam has a saved token, no password needed"><span class="lamp ready"></span>Instant</div>`
    : `<div class="state" title="No saved token yet, Steam will ask for the password once"><span class="lamp warn"></span>Needs password</div>`;
}

function renderGrid() {
  const grid = $("#grid");
  const list = visibleAccounts();
  $("#empty").hidden = list.length > 0 || !S.data.steam_found;
  $(".rack-head").hidden = list.length === 0;

  grid.innerHTML = list.map((acc, i) => {
    const m = acc.meta;
    const tags = [];
    if (m.label) tags.push(`<span class="tag" data-tint="${esc(m.color)}">${esc(m.label)}</span>`);
    if (acc.vault.shared_secret) tags.push(`<span class="tag tag-plain">2FA</span>`);

    return `
    <article class="row ${acc.is_active ? "is-active" : ""} ${m.color ? "has-tint" : ""}" data-id="${acc.steam64}"
             data-tint="${esc(m.color)}" draggable="true">
      <span class="ch ${i < 9 ? "" : "none"}" title="${i < 9 ? `Press ${i + 1} to switch` : ""}">${i < 9 ? String(i + 1).padStart(2, "0") : "--"}</span>
      <div class="who">
        ${avatarHtml(acc)}
        <div class="who-text">
          <div class="who-line"><b>${esc(acc.persona_name)}</b>${tags.join("")}</div>
          <span class="acct">${esc(acc.account_name)}</span>
          ${m.note ? `<p class="row-note">${esc(m.note)}</p>` : ""}
        </div>
      </div>
      ${signInState(acc)}
      <div class="used">${relTime(acc.last_login)}${m.switches ? `<small>${plural(m.switches, "switch", "switches")}</small>` : ""}</div>
      <div class="row-actions">
        <button class="star ${m.favorite ? "on" : ""}" data-star="${acc.steam64}"
                title="${m.favorite ? "Unpin from the top" : "Pin to the top"}" aria-label="Pin to the top">${icon("star")}</button>
        <button class="key key-icon" data-open="${acc.steam64}" title="Details" aria-label="Details for ${esc(acc.persona_name)}">${icon("sliders")}</button>
        <button class="key key-primary" data-switch="${acc.steam64}" ${acc.is_active ? "disabled" : ""}>
          ${acc.is_active ? "Current" : "Switch"}
        </button>
      </div>
    </article>`;
  }).join("");
  paint(grid);

  const total = S.data.accounts.length;
  $("#rack-foot").textContent = total
    ? `${plural(total, "account")}. Press 1 to 9 to switch, drag a row to change the order.`
    : "";
}

// ---- Drag to reorder ----
// The first drag also sets the sort to Custom, otherwise the saved order would be
// hidden straight away by the active sort.
let dragId = null;

$("#grid").addEventListener("dragstart", (e) => {
  const row = e.target.closest(".row");
  if (!row) return;
  dragId = row.dataset.id;
  row.classList.add("is-dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragId);
});

$("#grid").addEventListener("dragover", (e) => {
  if (!dragId) return;
  e.preventDefault();
  const over = e.target.closest(".row");
  const dragged = $(`.row[data-id="${dragId}"]`);
  if (!over || !dragged || over === dragged) return;
  const box = over.getBoundingClientRect();
  const after = e.clientY > box.top + box.height / 2;
  over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
});

$("#grid").addEventListener("dragend", async () => {
  $(`.row[data-id="${dragId}"]`)?.classList.remove("is-dragging");
  if (!dragId) return;
  dragId = null;

  const order = $$("#grid .row").map((r) => r.dataset.id);
  try {
    await api("/api/reorder", { order });
    if (S.data.settings.sort_mode !== "favorite") {
      await saveSetting("sort_mode", "favorite");
      toast("Order saved, sorting set to Custom");
    } else {
      await refresh();
    }
  } catch (err) { toast(err.message, "err"); }
});

// ---- Render: settings ----
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

// ---- Switching ----
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

const LEVEL_LAMP = { danger: "bad", warn: "warn", info: "", ok: "on" };

function issueHtml(item, extra = "") {
  return `<div class="issue lv-${item.level}"><span class="lamp ${LEVEL_LAMP[item.level] ?? ""}"></span>
    <div class="issue-body"><b>${esc(item.title)}</b><span>${esc(item.detail)}</span></div>${extra}</div>`;
}

function confirmSwitch(acc, risks) {
  return new Promise((resolve) => {
    const checks = risks.map((r) => issueHtml(r)).join("");

    $("#dialog").innerHTML = `
      <h3>Switch to ${esc(acc.persona_name)}?</h3>
      <p>Steam closes and starts again as <code>${esc(acc.account_name)}</code>.
      ${acc.has_token
        ? "It signs in by itself, no password needed."
        : "This account has no saved token yet, so Steam asks for its password once."}</p>
      ${checks ? `<div class="dialog-checks">${checks}</div>` : ""}
      <div class="dialog-foot">
        <button class="key" data-act="cancel">Cancel</button>
        <button class="key ${risks.some((r) => r.level === "danger") ? "key-warn" : "key-primary"}" data-act="ok">
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
  paint($(".switcher"));
  $("#sw-name").textContent = acc.persona_name;
  $("#sw-sub").textContent = `switching to ${acc.account_name}`;
  $("#sw-steps").innerHTML = "";
  $("#sw-close").hidden = true;
  $("#sw-spinner").hidden = false;
  $("#overlay").hidden = false;
  $(`.row[data-id="${steam64}"]`)?.classList.add("is-switching");

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
    $("#sw-elapsed").textContent = `${snap.elapsed.toFixed(1)} s`;
    if (snap.state === "done" || snap.state === "error") {
      clearInterval(poll);
      finishSwitch(snap.state === "done");
    }
  }, 350);
}

const STEP_CODE = { ok: "[ OK ]", warn: "[WARN]", error: "[FAIL]" };

function renderSteps(steps) {
  $("#sw-steps").innerHTML = steps.map((s) =>
    `<li class="step st-${s.status}"><span class="step-code">${STEP_CODE[s.status] || "[ .. ]"}</span><span>${esc(s.message)}</span></li>`
  ).join("");
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

// ---- Drawer ----
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
      <div class="who-text">
        <b>${esc(acc.persona_name)}</b>
        <span class="acct">${esc(acc.account_name)}</span>
      </div>
      <button class="key key-icon key-quiet" data-act="close" aria-label="Close">${icon("x")}</button>
    </div>

    <div class="drawer-actions">
      <button class="key key-primary" data-act="switch" ${acc.is_active ? "disabled" : ""}>
        ${acc.is_active ? "Signed in now" : "Switch to this account"}
      </button>
      <button class="key" data-act="profile" title="Open the Steam profile">Profile</button>
    </div>

    <div class="dsection">
      <h3>Identity</h3>
      ${idRow("SteamID64", acc.ids.steam64)}
      ${idRow("SteamID", acc.ids.steam2)}
      ${idRow("SteamID3", acc.ids.steam3)}
      ${idRow("Account ID", acc.ids.account_id)}
      ${idRow("Login token", acc.has_token ? "saved, switches instantly" : "none, one sign-in needed")}
      ${idRow("Last sign-in", relTime(acc.last_login))}
    </div>

    <div class="dsection">
      <h3>Label and note</h3>
      <div class="field-row">
        <div class="field"><label for="d-label">Label</label>
          <input id="d-label" value="${esc(m.label)}" placeholder="Main, Smurf, Trading" maxlength="24"></div>
        <div class="field field-color"><label for="d-color">Colour</label>
          <input id="d-color" type="color" value="${esc(m.color || "#ff6a2b")}"></div>
      </div>
      <div class="field"><label for="d-note">Note</label>
        <textarea id="d-note" placeholder="Anything worth remembering about this account" maxlength="400">${esc(m.note)}</textarea></div>
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
               <div><div class="guard-code" id="guard-code">-----</div>
                 <span class="guard-left" id="guard-left">loading</span></div>
               <button class="key key-sm" data-act="guard-copy" aria-label="Copy the code">${icon("copy")}</button>
             </div>
             <button class="key key-sm key-quiet" data-act="guard-remove">Remove the shared secret</button>`
          : `<div class="field"><label for="d-secret">Shared secret from your <code>.maFile</code> (Steam Desktop Authenticator)</label>
               <input id="d-secret" placeholder="shared_secret, base64" spellcheck="false"></div>
             <button class="key key-sm" data-act="guard-save">Store the secret</button>`}
      </div>
    </div>` : ""}

    ${vaultOn && !acc.has_token ? `
    <div class="dsection">
      <h3>Password</h3>
      <p class="note">Sealed with Windows DPAPI and only ever put on the clipboard. It is never passed
        to Steam on the command line, where other programs could read it.</p>
      ${acc.vault.password
        ? `<div class="field-row">
             <button class="key key-sm" data-act="pw-copy">${icon("copy")} Copy the password</button>
             <button class="key key-sm key-quiet" data-act="pw-remove">Remove</button>
           </div>`
        : `<div class="field"><input id="d-pw" type="password" placeholder="Password for this account" spellcheck="false" aria-label="Password"></div>
           <button class="key key-sm" data-act="pw-save">Store the password</button>`}
    </div>` : ""}

    <div class="dsection">
      <h3>Games on this PC</h3>
      <div class="games" id="d-games">${S.games[acc.steam64]
        || `<p class="games-sum">Reading Steam's files</p>`}</div>
    </div>

    <div class="dsection">
      <h3>Remove</h3>
      <button class="key key-sm key-danger" data-act="forget">Forget this account's Switcher data</button>
      <span class="danger-note">Clears the label, note and stored secrets. The Steam account is not touched.</span>
    </div>`;
  paint($("#drawer-inner"));
}

const idRow = (label, value) =>
  `<div class="idrow" data-copy="${esc(value)}" title="Copy"><span>${esc(label)}</span><code>${esc(value)}</code>${icon("copy")}</div>`;

async function loadGames(steam64) {
  try {
    const data = await api(`/api/games/${steam64}?limit=10`);

    let html;
    if (!data.games.length) {
      html = `<p class="games-sum">No games played on this PC with this account.</p>`;
    } else {
      html = `<p class="games-sum">
          <b>${data.total}</b> games · <b>${data.installed_count}</b> installed ·
          <b>${hours(data.total_minutes)}</b> played on this PC
          ${data.suspect_count ? `<span class="warn">${plural(data.suspect_count, "entry", "entries")} with an impossible play time left out of the total</span>` : ""}
        </p>` + data.games.map((g) => `
        <div class="game">
          <span class="game-name" title="${esc(g.name)}">${esc(g.name)}</span>
          ${g.suspect ? `<span class="game-suspect" title="Steam's local counter for this game is broken">${icon("alert")}</span>` : ""}
          <span class="game-time">${g.minutes ? hours(g.minutes) : relTime(g.last_played)}</span>
          ${g.installed ? `<button class="game-play" data-launch="${g.appid}"
              title="Switch to this account and start ${esc(g.name)}" aria-label="Start ${esc(g.name)}">${icon("play")}</button>` : ""}
        </div>`).join("");
    }

    S.games[steam64] = html;
    if (S.drawerId === steam64 && $("#d-games")) $("#d-games").innerHTML = html;
  } catch { /* drawer closed or Steam moved */ }
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
      $("#guard-left").textContent = `new code in ${g.remaining} s`;
      const circumference = 2 * Math.PI * 14;
      $("#guard-arc").style.strokeDashoffset = circumference * (1 - g.remaining / g.period);
    } catch { /* try again on the next tick */ }
  };
  tick();
  S.guardTimer = setInterval(tick, 1000);
}

// ---- History and Doctor ----
async function loadHistory() {
  const { history, stats } = await api("/api/history");
  $("#stat-row").innerHTML = [
    [stats.total_switches, "switches"],
    [stats.avg_seconds ? `${stats.avg_seconds} s` : "-", "average"],
    [stats.fastest_seconds ? `${stats.fastest_seconds} s` : "-", "fastest"],
    [stats.most_used || "-", "most used"],
  ].map(([v, l]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");

  $("#timeline").innerHTML = history.length
    ? history.slice(0, 40).map((h) => `
      <div class="tl">
        <span class="lamp ${h.ok ? "on" : "bad"}"></span>
        <div class="tl-body">
          <b>${esc(h.persona || h.account_name || h.steam64)}</b>
          <span>${h.ok ? `switched in ${h.duration} s` : esc(h.error || "failed")}</span>
        </div>
        <span class="tl-time">${relTime(h.at)}</span>
      </div>`).join("")
    : `<p class="panel-note">No switches yet.</p>`;

  const { backups } = await api("/api/backups");
  $("#backups").innerHTML = backups.length
    ? backups.map((b) => `
      <div class="bk">
        <div class="bk-body">
          <b>${esc(b.name)}</b>
          <span>auto-login was ${esc(b.auto_login_user || "not set")} · ${relTime(b.created)}</span>
        </div>
        <button class="key key-sm" data-restore="${esc(b.name)}">Restore</button>
      </div>`).join("")
    : `<p class="panel-note">No restore points yet.</p>`;
}

async function loadDoctor() {
  const { issues } = await api("/api/health");
  $("#doctor-badge").hidden = !issues.some((i) => i.level === "danger" || i.level === "warn");

  $("#issues").innerHTML = issues.length
    ? issues.map((i) => issueHtml(i,
        i.fixable ? `<button class="key key-sm" data-fix="${esc(i.code)}">Fix</button>` : "")).join("")
    : issueHtml({ level: "ok", title: "Nothing to fix",
        detail: "Steam is where it should be and every account is ready to switch." });
}

async function loadLegacy() {
  try {
    const data = await api("/api/legacy/scan");
    const note = $("#legacy-note");
    if (!data.found) {
      note.textContent = "No Steam.bat found next to this app. If you used a batch switcher before, put it in this folder to bring its accounts over.";
      $("#legacy-list").innerHTML = "";
      return;
    }
    const risky = data.accounts.filter((a) => a.has_password).length;
    note.innerHTML = `Found <code>${esc(data.path.split("\\").pop())}</code> with
      ${plural(data.accounts.length, "account")}.
      ${risky ? `<b>${plural(risky, "password")} in plain text</b> in that file. Anyone who has had a copy
      of it, or of a cloud folder it synced to, can read them: change them at
      <code>store.steampowered.com/account</code> and delete the file.` : ""}`;

    $("#legacy-list").innerHTML = data.accounts.map((a) => `
      <div class="lg">
        <span class="lamp ${a.matched ? "on" : ""}"></span>
        <div class="lg-body">
          <b>${esc(a.username)}</b>
          <span>${a.matched
            ? `matches ${esc(a.persona_name)}`
            : "not signed in on this PC yet"}${a.password_was_broken
            ? " · this password has <code>&amp;</code> or <code>^</code> in it, which cmd.exe breaks, so the old script could never sign in with it"
            : ""}</span>
        </div>
      </div>`).join("");
  } catch { /* Steam not found; the settings panel already says so */ }
}

// ---- Command palette ----
const PALETTE_ACTIONS = [
  { label: "Go to Accounts", icon: "users", run: () => setView("accounts") },
  { label: "Go to History", icon: "clock", run: () => setView("history") },
  { label: "Run the health check", icon: "shield", run: () => setView("doctor") },
  { label: "Go to Settings", icon: "cog", run: () => setView("settings") },
  { label: "Refresh", icon: "refresh", run: refresh },
  { label: "Save a restore point", icon: "shield",
    run: async () => { const r = await api("/api/backup", {}); toast(`Saved restore point ${r.name}`); } },
  { label: "Close Steam", icon: "power",
    run: async () => { toast("Asking Steam to close"); const r = await api("/api/steam/shutdown", {});
      toast(r.forced ? "Steam had to be forced closed" : "Steam closed", r.forced ? "warn" : "ok"); refresh(); } },
  { label: "Start Steam", icon: "play",
    run: async () => { const r = await api("/api/steam/launch", {}); toast(r.message); } },
  { label: "Light or dark theme", icon: "sliders",
    run: () => saveSetting("theme", S.data.settings.theme === "light" ? "dark" : "light") },
  { label: "Quit Steam Switcher", icon: "x", run: () => api("/api/quit") },
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
    </div>`).join("") || `<div class="palette-empty">Nothing matches "${esc(query)}".</div>`;
  paint($("#palette-list"));
}

function movePalette(delta) {
  if (!paletteItems.length) return;
  paletteIndex = (paletteIndex + delta + paletteItems.length) % paletteItems.length;
  $$("#palette-list .pitem").forEach((el, i) => el.classList.toggle("on", i === paletteIndex));
  $("#palette-list .pitem.on")?.scrollIntoView({ block: "nearest" });
}

function runPalette(index) {
  const item = paletteItems[index];
  if (!item) return;
  closePalette();
  Promise.resolve(item.run()).catch((e) => toast(e.message, "err"));
}

// ---- Settings ----
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

// #settings opens a view, #account/<steamid64> opens that account's details
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

// ---- Events ----
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
    return toast("Copied");
  }

  const launch = hit("data-launch");
  if (launch) {
    // already signed in: just start the game
    if (accountById(S.drawerId)?.is_active) {
      await api("/api/open", { target: `steam://rungameid/${launch}` });
      return toast("Starting the game");
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
    toast(`Saved restore point ${r.name}`);
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
        toast("Shared secret stored");
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
        return toast(`Code copied, clipboard clears in ${r.wipes_in} s`);
      }
      case "pw-save": {
        const value = $("#d-pw").value;
        if (!value) return;
        await api("/api/vault", { steam64: id, kind: "password", value });
        toast("Password stored");
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
        return toast(`Password copied, clipboard clears in ${r.wipes_in} s`);
      }
      case "forget":
        await api("/api/forget", { steam64: id });
        toast("Switcher data cleared for this account");
        closeDrawer();
        return refresh();
    }
  } catch (err) { toast(err.message, "err"); }
}

// writes are debounced so typing a note does not call the API on every key
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

// ---- Keyboard ----
document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName);
  const paletteOpen = !$("#palette").hidden;

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

  if (typing || !$("#modal").hidden || !$("#overlay").hidden) return;

  if (e.key === "/") { e.preventDefault(); return $("#search").focus(); }
  if (e.key.toLowerCase() === "r" && !e.ctrlKey) return refresh();
  if (e.key >= "1" && e.key <= "9" && S.view === "accounts") {
    const acc = visibleAccounts()[Number(e.key) - 1];
    if (acc && !acc.is_active) requestSwitch(acc.steam64);
  }
});

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (S.data?.settings.theme === "auto") applyTheme("auto");
});

// ---- Start ----
window.addEventListener("hashchange", applyHash);

(async function boot() {
  await refresh();
  startPolling();
  loadDoctor().catch(() => {});
  loadLegacy();
  applyHash();
})();
