// drivecast frontend v2 — cached library, tiles, seasons/episodes, hash routing.
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  library: [],          // cached title records from /api/library
  byId: {},             // id -> record
  filter: "all",        // all | movie | show
  query: "",            // client-side search over the library
  selectedDrives: [],
  // browse (advanced) sub-state
  drives: [],
  driveName: {},
  driveId: null,
  folderId: null,
  crumbs: [],
  nextPageToken: null,
  browseView: "drives", // drives | browse | search
};

// ---------- utilities ----------
async function api(path, opts) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const msg = (data && (data.message || data.error)) || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function fmtTime(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function escapeHTML(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function isFolder(f) { return f.mimeType === "application/vnd.google-apps.folder"; }

function show(el, on) { el.classList.toggle("hidden", !on); }

function showView(name) {
  show($("libraryView"), name === "library");
  show($("detailView"), name === "detail");
  show($("settingsView"), name === "settings");
  show($("browseView"), name === "browse");
}

// ---------- poster / placeholder ----------
function posterMarkup(rec) {
  const ph = `<div class="ph-title">${escapeHTML(rec.title)}</div>` +
    (rec.year ? `<div class="ph-year">${rec.year}</div>` : "");
  if (rec.poster) {
    return { cls: "", html: `<img loading="lazy" src="/api/poster/${encodeURIComponent(rec.poster)}" alt=""
      onerror="this.parentElement.classList.add('placeholder');this.remove();
               this.parentElement.insertAdjacentHTML('beforeend', this.dataset.ph||'')" data-ph='${escapeHTML(ph)}'>` };
  }
  return { cls: " placeholder", html: ph };
}

// ---------- library tiles ----------
function titleCard(rec) {
  const card = document.createElement("div");
  card.className = "card video";
  const p = posterMarkup(rec);
  const badge = rec.type === "show"
    ? `<span class="badge tv">TV</span>` : "";
  const sub = rec.type === "show"
    ? `${(rec.seasons || []).length} season${(rec.seasons || []).length === 1 ? "" : "s"}`
    : (rec.year || "");
  card.innerHTML = `
    <div class="poster${p.cls}">${badge}${p.html}</div>
    <div class="label">${escapeHTML(rec.title)}</div>
    <div class="sub">${escapeHTML(sub)}</div>`;
  card.addEventListener("click", () => { location.hash = "#/title/" + encodeURIComponent(rec.id); });
  return card;
}

function continueCard(item) {
  const card = document.createElement("div");
  card.className = "card video";
  const pct = Math.max(2, Math.min(98, item.percent || 0));
  card.innerHTML = `
    <div class="poster placeholder">
      <div class="ph-title">${escapeHTML(item.name)}</div>
      <div class="ph-year">${fmtTime(item.position)} watched</div>
      <div class="progress"><span style="width:${pct}%"></span></div>
    </div>
    <div class="label">${escapeHTML(item.name)}</div>
    <div class="sub">${Math.round(item.percent)}%</div>`;
  card.addEventListener("click", () =>
    playFile({ id: item.file_id, name: item.name, drive_id: item.drive_id, parent_id: item.parent_id },
             item.duration ? item.duration * 1000 : null, true));
  return card;
}

// ---------- data loads ----------
async function loadLibrary() {
  try {
    const data = await api("/api/library");
    state.library = data.titles || [];
    state.selectedDrives = data.selected_drives || [];
    state.byId = {};
    for (const rec of state.library) state.byId[rec.id] = rec;
    if (data.scanning) startScanWatch();
  } catch (e) {
    toast("Could not load library: " + e.message, "error");
  }
}

function matchesFilter(rec) {
  if (state.filter !== "all" && rec.type !== state.filter) return false;
  if (state.query) {
    const q = state.query.toLowerCase();
    if (!(rec.title || "").toLowerCase().includes(q)) return false;
  }
  return true;
}

function renderLibrary() {
  const grid = $("libGrid");
  grid.innerHTML = "";
  const items = state.library
    .filter(matchesFilter)
    .sort((a, b) => (a.title || "").localeCompare(b.title || ""));

  // Empty states / call to action.
  if (!state.selectedDrives.length) {
    show($("libSection"), false);
    showCta(`<h2>Pick your drives</h2>
      <p>Choose which Shared Drives to include, then drivecast builds your library.</p>
      <a class="btn primary" href="#/settings">Open Settings</a>`);
    return;
  }
  if (!state.library.length) {
    show($("libSection"), false);
    showCta(`<h2>Library is empty</h2>
      <p>No titles cached yet. Run a refresh to scan your selected drives.</p>
      <button class="btn primary" onclick="triggerRefresh()">Refresh now</button>`);
    return;
  }
  show($("cta"), false);
  show($("libSection"), true);
  if (!items.length) {
    grid.innerHTML = `<div class="empty">No titles match “${escapeHTML(state.query)}”.</div>`;
    return;
  }
  for (const rec of items) grid.appendChild(titleCard(rec));
}

function showCta(html) {
  $("cta").innerHTML = html;
  show($("cta"), true);
}

async function loadContinue() {
  try {
    const data = await api("/api/continue");
    const items = data.items || [];
    const sec = $("continueSection");
    const row = $("continueRow");
    row.innerHTML = "";
    if (!items.length) { show(sec, false); return; }
    for (const it of items) row.appendChild(continueCard(it));
    show(sec, true);
  } catch (_) { /* non-fatal */ }
}

// ---------- refresh ----------
let scanTimer = null;
async function triggerRefresh() {
  try {
    const res = await api("/api/refresh", { method: "POST" });
    if (res.running && !res.started) toast("A refresh is already running.");
    else toast("Refreshing library…");
    startScanWatch();
  } catch (e) {
    if (e.status === 400) toast(e.message, "error");
    else toast("Refresh failed: " + e.message, "error");
  }
}
window.triggerRefresh = triggerRefresh;

function startScanWatch() {
  $("refreshBtn").classList.add("spinning");
  if (scanTimer) return;
  scanTimer = setInterval(pollScan, 1200);
  pollScan();
}

async function pollScan() {
  let st;
  try { st = await api("/api/refresh/status"); } catch (_) { return; }
  const bar = $("scanBar");
  if (st.running) {
    show(bar, true);
    bar.textContent = `Scanning drives… ${st.scanned}/${st.total}` +
      (st.added ? ` · +${st.added} new` : "");
  } else {
    clearInterval(scanTimer); scanTimer = null;
    $("refreshBtn").classList.remove("spinning");
    show(bar, false);
    if (st.error) toast("Scan finished with issues: " + st.error, "error");
    await loadLibrary();
    if (currentRoute() === "library") { renderLibrary(); loadContinue(); }
  }
}

// ---------- detail view ----------
async function openDetail(id) {
  showView("detail");
  const body = $("detailBody");
  let rec = state.byId[id];
  if (!rec) {
    try { rec = await api("/api/title/" + encodeURIComponent(id)); }
    catch (_) { body.innerHTML = `<div class="empty">Title not found.</div>`; return; }
  }
  if (rec.type === "show") renderShowDetail(rec);
  else renderMovieDetail(rec);
}

function detailHeader(rec) {
  const p = posterMarkup(rec);
  return `
    <div class="detail-hero">
      <div class="detail-poster poster${p.cls}">${p.html}</div>
      <div class="detail-meta">
        <h1>${escapeHTML(rec.title)}</h1>
        <div class="detail-sub">${rec.year || ""}${rec.type === "show"
          ? " · " + (rec.seasons || []).length + " season" + ((rec.seasons || []).length === 1 ? "" : "s")
          : ""}</div>
        <p class="detail-overview">${escapeHTML(rec.overview || "")}</p>
        <div id="detailActions" class="detail-actions"></div>
      </div>
    </div>`;
}

function renderMovieDetail(rec) {
  $("detailBody").innerHTML = detailHeader(rec);
  const actions = $("detailActions");
  const play = document.createElement("button");
  play.className = "btn primary";
  play.textContent = "Play";
  play.addEventListener("click", () => playFile(
    { id: rec.file_id, name: rec.title, drive_id: rec.drive_id, parent_id: rec.folder_id },
    rec.duration_ms || null));
  actions.appendChild(play);
}

function renderShowDetail(rec) {
  $("detailBody").innerHTML = detailHeader(rec) + `
    <div class="season-select">
      <label>Season</label>
      <select id="seasonSel"></select>
    </div>
    <div id="episodeList" class="episodes"></div>`;
  const seasons = rec.seasons || [];
  const sel = $("seasonSel");
  seasons.forEach((s, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = s.season === 0 ? "Specials" : "Season " + s.season;
    sel.appendChild(opt);
  });
  // Default to the season of the in-progress episode, if any.
  sel.addEventListener("change", () => renderEpisodes(rec, seasons[+sel.value]));
  renderEpisodes(rec, seasons[0]);
}

function renderEpisodes(rec, season) {
  const list = $("episodeList");
  list.innerHTML = "";
  if (!season) return;
  for (const ep of season.episodes) {
    const row = document.createElement("div");
    row.className = "episode";
    const num = ep.episode != null ? `E${String(ep.episode).padStart(2, "0")}` : "";
    row.innerHTML = `
      <span class="ep-num">${num}</span>
      <span class="ep-title">${escapeHTML(ep.title || ep.name)}</span>
      <span class="ep-dur">${ep.duration_ms ? fmtTime(ep.duration_ms / 1000) : ""}</span>
      <span class="ep-play">▶</span>`;
    row.addEventListener("click", () => playFile(
      { id: ep.file_id, name: ep.name, drive_id: rec.drive_id, parent_id: ep.parent_id },
      ep.duration_ms || null));
    list.appendChild(row);
  }
}

// ---------- settings view ----------
async function openSettings() {
  showView("settings");
  $("settingsMsg").textContent = "";
  const list = $("driveList");
  list.innerHTML = `<div class="spinner">Loading drives…</div>`;
  let drives = [], settings = {};
  try { settings = await api("/api/settings"); } catch (_) {}
  try { drives = (await api("/api/drives")).drives || []; }
  catch (e) { list.innerHTML = `<div class="empty">Could not list drives: ${escapeHTML(e.message)}</div>`; return; }
  const selected = new Set(settings.selected_drives || []);
  $("autoRefresh").checked = !!settings.auto_refresh_on_startup;
  list.innerHTML = "";
  if (!drives.length) { list.innerHTML = `<div class="empty">No Shared Drives found.</div>`; return; }
  for (const d of drives) {
    const label = document.createElement("label");
    label.className = "drive-row";
    label.innerHTML = `<input type="checkbox" value="${escapeHTML(d.id)}" ${selected.has(d.id) ? "checked" : ""}>
      <span>${escapeHTML(d.name || d.id)}</span>`;
    list.appendChild(label);
  }
}

async function saveSettings() {
  const selected = [...$("driveList").querySelectorAll("input:checked")].map((c) => c.value);
  const auto = $("autoRefresh").checked;
  $("settingsMsg").textContent = "Saving…";
  try {
    const res = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_drives: selected, auto_refresh_on_startup: auto }),
    });
    $("settingsMsg").textContent = "Saved.";
    state.selectedDrives = res.selected_drives || [];
    if (res.refresh_started) { toast("Drives changed — refreshing library…"); startScanWatch(); }
  } catch (e) {
    $("settingsMsg").textContent = "Save failed: " + e.message;
  }
}

// ---------- play ----------
let pendingPlay = null;

async function playFile(f, durationMs, skipResumeCheck) {
  const fileId = f.id;
  const name = f.name;
  const durMs = durationMs || null;
  const driveId = f.drive_id || state.driveId;
  const parentId = f.parent_id || state.folderId;

  let resumeAt = 0;
  if (!skipResumeCheck) {
    try {
      const cont = await api("/api/continue");
      const hit = (cont.items || []).find((x) => x.file_id === fileId);
      if (hit) resumeAt = hit.position || 0;
    } catch (_) {}
  }
  if (resumeAt > 5 && !skipResumeCheck) {
    pendingPlay = { fileId, name, durMs, driveId, parentId };
    $("modalBody").textContent =
      `You were at ${fmtTime(resumeAt)}. Resume or start from the beginning?`;
    show($("modal"), true);
    return;
  }
  await launch(fileId, name, durMs, driveId, parentId, false);
}

async function launch(fileId, name, durMs, driveId, parentId, startOver) {
  try {
    const res = await api("/api/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_id: fileId, name, duration_ms: durMs,
        drive_id: driveId, parent_id: parentId, start_over: !!startOver,
      }),
    });
    const from = res.resumed_from > 1 ? ` (resumed at ${fmtTime(res.resumed_from)})` : "";
    toast(`Playing in ${res.player}${from}`, "success");
    if (res.player === "vlc") showVlcBanner();
    setTimeout(loadContinue, 1500);
  } catch (e) {
    if (e.status === 501) toast(e.message, "error");
    else toast("Play failed: " + e.message, "error");
  }
}

function showVlcBanner() {
  const b = $("banner");
  b.innerHTML = "Playing in VLC — resume tracking is unavailable. " +
    "Install mpv for automatic resume: <code>brew install mpv</code>";
  show(b, true);
}

$("btnResume").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, false);
});
$("btnStartOver").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, true);
});
$("btnCancel").addEventListener("click", () => { show($("modal"), false); pendingPlay = null; });

// ==================== Browse (advanced) ====================
async function loadDrives() {
  try {
    const data = await api("/api/drives");
    state.drives = data.drives || [];
    state.driveName = {};
    const row = $("drivesRow");
    row.innerHTML = "";
    for (const d of state.drives) {
      state.driveName[d.id] = d.name;
      const c = document.createElement("div");
      c.className = "card folder";
      c.innerHTML = `<div class="poster"><span class="folder-icon">🎞️</span></div>
                     <div class="label">${escapeHTML(d.name)}</div>`;
      c.addEventListener("click", () => {
        state.crumbs = [{ name: d.name, driveId: d.id, folderId: null }];
        location.hash = `#/browse/drive/${d.id}`;
      });
      row.appendChild(c);
    }
  } catch (e) {
    toast("Could not load drives: " + e.message, "error");
  }
}

function browseVideoCard(f) {
  const card = document.createElement("div");
  card.className = "card video";
  const ph = `<div class="ph-title">${escapeHTML(f.name)}</div>`;
  let inner = "";
  let cls = " placeholder";
  if (f.thumbnailLink) {
    cls = "";
    const u = `/api/poster/_?thumb=${encodeURIComponent(f.thumbnailLink)}`;
    inner = `<img loading="lazy" src="${u}" alt="" data-ph='${escapeHTML(ph)}'
      onerror="this.parentElement.classList.add('placeholder');this.remove();
               this.parentElement.insertAdjacentHTML('beforeend', this.dataset.ph||'')">`;
  } else {
    inner = ph;
  }
  card.innerHTML = `
    <div class="poster${cls}">${inner}</div>
    <div class="label">${escapeHTML(f.name)}</div>`;
  card.addEventListener("click", () => playFile(f,
    (f.videoMediaMetadata && f.videoMediaMetadata.durationMillis) || null));
  return card;
}

function folderCard(f) {
  const card = document.createElement("div");
  card.className = "card folder";
  card.innerHTML = `
    <div class="poster"><span class="folder-icon">📁</span></div>
    <div class="label">${escapeHTML(f.name)}</div>`;
  card.addEventListener("click", () => {
    state.crumbs.push({ name: f.name, driveId: state.driveId, folderId: f.id });
    location.hash = `#/browse/drive/${state.driveId}/folder/${f.id}`;
  });
  return card;
}

function renderBrowseFiles(files, append) {
  const grid = $("grid");
  if (!append) grid.innerHTML = "";
  for (const f of files.filter(isFolder)) grid.appendChild(folderCard(f));
  for (const f of files.filter((x) => !isFolder(x))) grid.appendChild(browseVideoCard(f));
}

async function doBrowse(driveId, folderId, append) {
  state.browseView = "browse";
  state.driveId = driveId;
  state.folderId = folderId;
  show($("drivesSection"), false);
  show($("gridSection"), true);
  show($("empty"), false);
  renderBreadcrumb();
  const params = new URLSearchParams({ drive_id: driveId });
  if (folderId) params.set("folder_id", folderId);
  if (append && state.nextPageToken) params.set("page_token", state.nextPageToken);
  if (!append) $("grid").innerHTML = '<div class="spinner">Loading…</div>';
  try {
    const data = await api("/api/browse?" + params.toString());
    if (!append) $("grid").innerHTML = "";
    state.nextPageToken = data.nextPageToken || null;
    $("gridTitle").textContent = state.crumbs.length
      ? state.crumbs[state.crumbs.length - 1].name : (state.driveName[driveId] || "Browse");
    renderBrowseFiles(data.files || [], append);
    show($("loadMore"), !!state.nextPageToken);
    if (!append && !(data.files || []).length) {
      $("empty").textContent = "This folder is empty."; show($("empty"), true);
    }
  } catch (e) {
    $("grid").innerHTML = "";
    toast("Browse failed: " + e.message, "error");
  }
}

function renderBreadcrumb() {
  const bc = $("breadcrumb");
  if (!state.crumbs.length) { show(bc, false); return; }
  bc.innerHTML = "";
  const home = document.createElement("a");
  home.textContent = "Drives";
  home.addEventListener("click", () => { location.hash = "#/browse"; });
  bc.appendChild(home);
  state.crumbs.forEach((c, i) => {
    const sep = document.createElement("span");
    sep.className = "sep"; sep.textContent = "›";
    bc.appendChild(sep);
    const a = document.createElement("a");
    a.textContent = c.name;
    a.addEventListener("click", () => {
      state.crumbs = state.crumbs.slice(0, i + 1);
      location.hash = c.folderId
        ? `#/browse/drive/${c.driveId}/folder/${c.folderId}` : `#/browse/drive/${c.driveId}`;
    });
    bc.appendChild(a);
  });
  show(bc, true);
}

function openBrowseHome() {
  state.browseView = "drives";
  state.crumbs = [];
  state.nextPageToken = null;
  show($("drivesSection"), true);
  show($("gridSection"), false);
  show($("breadcrumb"), false);
  show($("empty"), false);
  loadDrives();
}

$("loadMore").addEventListener("click", () => {
  if (state.browseView === "browse") doBrowse(state.driveId, state.folderId, true);
});

// ---------- routing ----------
function currentRoute() {
  const h = location.hash || "#/";
  if (h.startsWith("#/title/")) return "detail";
  if (h.startsWith("#/settings")) return "settings";
  if (h.startsWith("#/browse")) return "browse";
  return "library";
}

function router() {
  const h = location.hash || "#/";
  const mTitle = h.match(/^#\/title\/(.+)$/);
  const mBrowseFolder = h.match(/^#\/browse\/drive\/([^/]+)\/folder\/([^/]+)$/);
  const mBrowseDrive = h.match(/^#\/browse\/drive\/([^/]+)$/);

  if (mTitle) {
    openDetail(decodeURIComponent(mTitle[1]));
  } else if (h.startsWith("#/settings")) {
    openSettings();
  } else if (mBrowseFolder) {
    showView("browse");
    doBrowse(decodeURIComponent(mBrowseFolder[1]), decodeURIComponent(mBrowseFolder[2]), false);
  } else if (mBrowseDrive) {
    showView("browse");
    const id = decodeURIComponent(mBrowseDrive[1]);
    if (!state.crumbs.length) state.crumbs = [{ name: state.driveName[id] || "Drive", driveId: id, folderId: null }];
    doBrowse(id, null, false);
  } else if (h.startsWith("#/browse")) {
    showView("browse");
    openBrowseHome();
  } else {
    showView("library");
    renderLibrary();
    loadContinue();
  }
}

// ---------- events ----------
$("homeBtn").addEventListener("click", () => { location.hash = "#/"; });
$("detailBack").addEventListener("click", () => history.back());
$("settingsBack").addEventListener("click", () => { location.hash = "#/"; });
$("browseBack").addEventListener("click", () => { location.hash = "#/"; });
$("refreshBtn").addEventListener("click", triggerRefresh);
$("saveSettings").addEventListener("click", saveSettings);

$("filters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip");
  if (!btn) return;
  state.filter = btn.dataset.filter;
  [...$("filters").children].forEach((c) => c.classList.toggle("active", c === btn));
  renderLibrary();
});

$("search").addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  if (currentRoute() === "library") renderLibrary();
  else location.hash = "#/";
});

window.addEventListener("hashchange", router);

// ---------- init ----------
(async function init() {
  await loadLibrary();
  router();
})();
