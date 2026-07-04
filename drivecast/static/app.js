// drivecast frontend v2 — cached library, tiles, seasons/episodes, hash routing.
"use strict";

const $ = (id) => document.getElementById(id);

// The app is split into sections, each with its own tab, accent colour and
// vocabulary. A drive is assigned to a section in Settings; unassigned =
// entertainment (records carry `section`). These built-ins are only the
// fallback — the real list (including any custom private section plugins) is
// loaded from /api/sections at init.
let SECTION_META = {
  entertainment: { label: "Entertainment", icon: "🍿", continue: "Continue Watching",
    lib: "Your Library",
    empty: "No entertainment titles yet — assign drives in Settings and refresh." },
  courses: { label: "Courses", icon: "🎓", accent: "#4ade80", accent2: "#86efac",
    continue: "Continue Learning", lib: "Your Courses",
    empty: "No course drive yet — assign one to Courses in Settings and it appears here.",
    season: "Module", episode: "Lesson" },
  podcasts: { label: "Podcasts", icon: "🎙", accent: "#c084fc", accent2: "#e0b3ff",
    continue: "Continue Watching", lib: "Your Podcasts",
    empty: "No podcast drive yet — assign one in Settings when you've added it." },
};
let SECTION_ORDER = Object.keys(SECTION_META);

async function loadSections() {
  try {
    const data = await api("/api/sections");
    const list = data.sections || [];
    if (!list.length) return;
    SECTION_META = {};
    SECTION_ORDER = [];
    for (const m of list) {
      SECTION_META[m.key] = m;
      SECTION_ORDER.push(m.key);
    }
  } catch (_) { /* fall back to the built-ins */ }
}

// On a phone/tablet the page is served over the LAN/tailnet (not loopback), so
// playback stays in the browser / hands off to VLC/Infuse rather than launching
// mpv on the Mac. remoteToken is fetched lazily from /api/remote (needed for the
// external-player deep links; the same-origin <video> uses the cookie instead).
const REMOTE_LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]"];

const state = {
  library: [],          // cached title records from /api/library
  byId: {},             // id -> record
  section: "entertainment", // active section tab
  remote: !REMOTE_LOCAL_HOSTS.includes(location.hostname),
  remoteToken: "",      // secret token for external-player stream URLs
  remoteInfo: null,     // cached /api/remote payload (urls, port, token)
  filter: "all",        // all | movie | show | documentary | other
  sort: "title",        // title | year | added | watched
  group: "none",        // none | type | drive
  query: "",            // client-side search over the library
  watchedMap: {},       // file_id -> last_played epoch (for "Recently watched")
  progress: {},         // file_id -> {percent, watched} (lesson/episode progress)
  selectedDrives: [],
  driveSections: {},    // drive_id -> section
  autoplayNext: true,   // autoplay next episode when one finishes
  // browse (advanced) sub-state
  drives: [],
  driveName: {},
  driveId: null,
  folderId: null,
  crumbs: [],
  nextPageToken: null,
  browseView: "drives", // drives | browse | search
};

function sectionOf(rec) { return rec.section || "entertainment"; }

// Sections become visible the moment any drive is assigned away from
// entertainment; until then the app looks exactly like it always did.
function sectionsActive() {
  return Object.values(state.driveSections || {}).some((s) => s && s !== "entertainment");
}

function setSection(sec) {
  if (!SECTION_META[sec]) sec = "entertainment";
  state.section = sec;
  document.body.dataset.section = sec;
  // Section accents come from the metadata (so custom plugin sections theme
  // themselves); entertainment clears back to the stylesheet default.
  const m = SECTION_META[sec] || {};
  if (m.accent) {
    document.body.style.setProperty("--accent", m.accent);
    document.body.style.setProperty("--accent-2", m.accent2 || m.accent);
  } else {
    document.body.style.removeProperty("--accent");
    document.body.style.removeProperty("--accent-2");
  }
  try { localStorage.setItem("dc.section", sec); } catch (_) {}
}

function renderTabs() {
  const nav = $("sectionTabs");
  if (!sectionsActive()) { show(nav, false); return; }
  nav.innerHTML = "";
  for (const sec of SECTION_ORDER) {
    const m = SECTION_META[sec];
    const a = document.createElement("a");
    a.className = "section-tab" + (sec === state.section ? " active" : "");
    a.innerHTML = `<span class="tab-icon">${m.icon}</span> ${m.label}`;
    a.addEventListener("click", () => { location.hash = "#/s/" + sec; });
    nav.appendChild(a);
  }
  show(nav, true);
}

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

// Extensions Safari/Chrome play inline in a <video>/<audio> element. Everything
// else (MKV, AVI, …) is handed off to VLC iOS / Infuse on remote devices.
const BROWSER_EXTS = [".mp4", ".m4v", ".mov", ".webm", ".mp3", ".m4a", ".m4b", ".aac", ".wav"];
function canPlayInBrowser(name) {
  const n = String(name || "").toLowerCase();
  return BROWSER_EXTS.some((ext) => n.endsWith(ext));
}

// Load (once, or force-reload) the remote-access info: token + tappable URLs.
// Failure-silent — off/loopback just leaves state.remoteToken empty.
async function ensureRemoteInfo(force) {
  if (state.remoteInfo && !force) return state.remoteInfo;
  try {
    const data = await api("/api/remote");
    state.remoteToken = data.token || "";
    state.remoteInfo = data;
  } catch (_) { if (force) state.remoteInfo = null; }
  return state.remoteInfo;
}

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
      onerror="this.parentElement.classList.add('placeholder');
               this.parentElement.insertAdjacentHTML('beforeend', this.dataset.ph||'');this.remove()" data-ph='${escapeHTML(ph)}'>` };
  }
  return { cls: " placeholder", html: ph };
}

// Quality pill (e.g. "4K", "1080p", "4K HDR") for a poster corner; "" if none.
function qualityPill(rec) {
  return rec.quality ? `<span class="pill">${escapeHTML(rec.quality)}</span>` : "";
}

// ---------- library tiles ----------
function countEpisodes(rec) {
  let n = 0;
  for (const s of rec.seasons || []) n += (s.episodes || []).length;
  return n;
}

// Fraction of a show/course's episodes marked watched (0..1).
function courseProgress(rec) {
  let total = 0, done = 0;
  for (const s of rec.seasons || [])
    for (const e of s.episodes || []) {
      if (!e.file_id) continue;
      total++;
      if ((state.progress[e.file_id] || {}).watched) done++;
    }
  return total ? done / total : 0;
}

// CSS conic-gradient progress ring for course tiles (0..1); "" when untouched.
function progressRing(p) {
  if (!p) return "";
  return `<span class="ring${p >= 1 ? " full" : ""}" style="--p:${Math.round(p * 100)}"
    title="${Math.round(p * 100)}% complete"></span>`;
}

function mediaPill(rec) {
  if (rec.media === "audio") return `<span class="pill">♪ audio</span>`;
  if (rec.media === "mixed") return `<span class="pill">♪ + ▶</span>`;
  return "";
}

function titleCard(rec) {
  const card = document.createElement("div");
  card.className = "card video";
  const p = posterMarkup(rec);
  const sec = sectionOf(rec);
  const isShow = rec.type === "show";
  let badge = "", pill = "", sub = "";
  // Movie-shaped records (loose files on a sections drive, or v1-migrated
  // records pre-rescan) fall through to the plain movie subtitle.
  if (sec === "courses" && isShow) {
    const prog = courseProgress(rec);
    pill = progressRing(prog);
    if (prog >= 1) badge = `<span class="badge done">✓ Done</span>`;
    const mods = (rec.seasons || []).length;
    sub = mods > 1 ? `${mods} modules · ${countEpisodes(rec)} lessons`
                   : `${countEpisodes(rec)} lessons`;
  } else if (sec === "podcasts" && isShow) {
    pill = mediaPill(rec);
    sub = `${countEpisodes(rec)} episode${countEpisodes(rec) === 1 ? "" : "s"}`;
  } else if (sec !== "entertainment" && isShow) {
    // Custom plugin sections: media pill + counts in the section's own nouns.
    pill = mediaPill(rec);
    const n = nomen(rec);
    const cnt = (rec.seasons || []).length;
    sub = `${cnt} ${n.season.toLowerCase()}${cnt === 1 ? "" : "s"} · ` +
          `${countEpisodes(rec)} ${n.episode.toLowerCase()}${countEpisodes(rec) === 1 ? "" : "s"}`;
  } else if (sec !== "entertainment") {
    pill = mediaPill(rec) || qualityPill(rec);
    sub = rec.year || "";
  } else {
    badge = rec.type === "show" ? `<span class="badge tv">TV</span>` : "";
    pill = qualityPill(rec);
    sub = rec.type === "show"
      ? `${(rec.seasons || []).length} season${(rec.seasons || []).length === 1 ? "" : "s"}`
      : (rec.year || "");
  }
  card.innerHTML = `
    <div class="poster${p.cls}">${badge}${pill}${p.html}</div>
    <div class="label">${escapeHTML(rec.title)}</div>
    <div class="sub">${escapeHTML(String(sub))}</div>`;
  card.addEventListener("click", () => { location.hash = "#/title/" + encodeURIComponent(rec.id); });
  return card;
}

function continueCard(item) {
  const card = document.createElement("div");
  card.className = "card video";
  const pct = Math.max(2, Math.min(98, item.percent || 0));
  const label = item.title || item.name;
  const progress = `<div class="progress"><span style="width:${pct}%"></span></div>`;
  const ph = `<div class="ph-title">${escapeHTML(label)}</div>` +
    `<div class="ph-year">${fmtTime(item.position)} watched</div>`;
  let cls = " placeholder", inner = ph;
  if (item.poster) {
    cls = "";
    inner = `<img loading="lazy" src="/api/poster/${encodeURIComponent(item.poster)}" alt=""
      onerror="this.parentElement.classList.add('placeholder');
               this.parentElement.insertAdjacentHTML('afterbegin', this.dataset.ph||'');this.remove()" data-ph='${escapeHTML(ph)}'>`;
  }
  card.innerHTML = `
    <div class="poster${cls}">${inner}${progress}</div>
    <div class="label">${escapeHTML(label)}</div>
    <div class="sub">${Math.round(item.percent)}% · ${fmtTime(item.position)} watched</div>`;
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

// Effective category: TMDB-derived when known, else the structural type
// (movie/show), so libraries scanned without a TMDB key still filter sanely.
function categoryOf(rec) {
  return rec.category || (rec.type === "show" ? "show" : "movie");
}

function matchesFilter(rec) {
  if (sectionOf(rec) !== state.section) return false;
  if (state.section === "entertainment" &&
      state.filter !== "all" && categoryOf(rec) !== state.filter) return false;
  if (state.query) {
    const q = state.query.toLowerCase();
    if (!(rec.title || "").toLowerCase().includes(q)) return false;
  }
  return true;
}

// Update filter-chip labels with live counts ("Documentaries · 7"); chips for
// empty categories are hidden (except All).
function updateFilterChips(items) {
  const counts = {};
  for (const rec of items) counts[categoryOf(rec)] = (counts[categoryOf(rec)] || 0) + 1;
  const labels = { all: "All", movie: "Movies", show: "TV Shows",
                   documentary: "Documentaries", other: "Other" };
  for (const chip of $("filters").children) {
    const f = chip.dataset.filter;
    const n = f === "all" ? items.length : (counts[f] || 0);
    chip.textContent = n && f !== "all" ? `${labels[f]} · ${n}` : labels[f];
    chip.classList.toggle("hidden", f !== "all" && !n && state.filter !== f);
  }
}

// ---------- sorting / grouping (client-side over cached library) ----------
function fileIdsOf(rec) {
  if (rec.type === "show") {
    const ids = [];
    for (const s of rec.seasons || [])
      for (const e of s.episodes || []) if (e.file_id) ids.push(e.file_id);
    return ids;
  }
  return rec.file_id ? [rec.file_id] : [];
}

function lastPlayedOf(rec) {
  let m = 0;
  for (const id of fileIdsOf(rec)) { const t = state.watchedMap[id] || 0; if (t > m) m = t; }
  return m;
}

function sortItems(items) {
  const arr = items.slice();
  const byTitle = (a, b) => (a.title || "").localeCompare(b.title || "");
  if (state.sort === "year") arr.sort((a, b) => ((b.year || 0) - (a.year || 0)) || byTitle(a, b));
  else if (state.sort === "added") arr.sort((a, b) => ((b.added_at || 0) - (a.added_at || 0)) || byTitle(a, b));
  else if (state.sort === "watched") arr.sort((a, b) => (lastPlayedOf(b) - lastPlayedOf(a)) || byTitle(a, b));
  else arr.sort(byTitle);
  return arr;
}

function driveLabel(id) { return state.driveName[id] || id || "Unknown drive"; }

function groupItems(items) {
  // Non-entertainment sections group into shelves by default ("Python",
  // "Audio Series", "Talks", ...) — the classifier sets rec.shelf.
  if (state.section !== "entertainment" && state.group === "none") {
    const map = {};
    for (const r of items) (map[r.shelf || ""] = map[r.shelf || ""] || []).push(r);
    const keys = Object.keys(map).sort((a, b) => a.localeCompare(b));
    if (keys.length === 1) return [{ key: null, title: null, items }];
    return keys.map((k) => ({ key: k || "_more", title: k || "More", items: map[k] }));
  }
  if (state.group === "type") {
    const movies = items.filter((r) => r.type === "movie");
    const shows = items.filter((r) => r.type === "show");
    const groups = [];
    if (movies.length) groups.push({ key: "movie", title: "Movies", items: movies });
    if (shows.length) groups.push({ key: "show", title: "TV Shows", items: shows });
    return groups;
  }
  if (state.group === "drive") {
    const map = {};
    for (const r of items) (map[r.drive_id || ""] = map[r.drive_id || ""] || []).push(r);
    return Object.keys(map)
      .sort((a, b) => driveLabel(a).localeCompare(driveLabel(b)))
      .map((k) => ({ key: k, title: driveLabel(k), items: map[k] }));
  }
  return [{ key: null, title: null, items }];
}

function renderLibrary() {
  // If sections were deactivated (all drives back on entertainment) while a
  // non-entertainment section was active/persisted, the hidden tab bar would
  // leave the library unreachable — snap back to entertainment.
  if (!sectionsActive() && state.section !== "entertainment") setSection("entertainment");
  renderTabs();
  $("continueHead").textContent = SECTION_META[state.section].continue;
  $("libHead").textContent = SECTION_META[state.section].lib;
  const grid = $("libGrid");
  grid.innerHTML = "";
  grid.className = "grid";
  const inSection = state.library.filter((r) => sectionOf(r) === state.section);
  show($("filters"), state.section === "entertainment");
  if (state.section === "entertainment") {
    updateFilterChips(inSection.filter((r) => !state.query ||
      (r.title || "").toLowerCase().includes(state.query.toLowerCase())));
  }
  let items = sortItems(state.library.filter(matchesFilter));

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
  if (!inSection.length) {
    show($("libSection"), false);
    showCta(`<h2>${SECTION_META[state.section].icon || ""} Nothing here yet</h2>
      <p>${escapeHTML(SECTION_META[state.section].empty ||
        "Assign a drive to this section in Settings.")}</p>
      <a class="btn primary" href="#/settings">Open Settings</a>`);
    return;
  }
  show($("cta"), false);
  show($("libSection"), true);
  if (!items.length) {
    grid.innerHTML = `<div class="empty">No titles match “${escapeHTML(state.query)}”.</div>`;
    return;
  }

  const groups = groupItems(items);
  if (groups.length === 1 && groups[0].key === null) {
    for (const rec of groups[0].items) grid.appendChild(titleCard(rec));
    return;
  }
  // Grouped: render a heading + its own grid per group.
  grid.classList.remove("grid");
  grid.classList.add("lib-grouped");
  for (const g of groups) {
    const sec = document.createElement("section");
    sec.className = "lib-group";
    const h = document.createElement("h3");
    h.className = "group-head";
    h.textContent = g.title;
    sec.appendChild(h);
    const gg = document.createElement("div");
    gg.className = "grid";
    for (const rec of g.items) gg.appendChild(titleCard(rec));
    sec.appendChild(gg);
    grid.appendChild(sec);
  }
}

// Lazy loaders for data the sort/group needs.
async function ensureWatchedMap() {
  try {
    const data = await api("/api/watched-map");
    state.watchedMap = data.map || {};
    state.progress = data.progress || {};
  } catch (_) { /* non-fatal */ }
}

async function ensureDriveNames() {
  if (Object.keys(state.driveName).length) return;
  try {
    const data = await api("/api/drives");
    for (const d of data.drives || []) state.driveName[d.id] = d.name;
  } catch (_) { /* non-fatal — falls back to raw ids */ }
}

function showCta(html) {
  $("cta").innerHTML = html;
  show($("cta"), true);
}

async function loadContinue() {
  try {
    const data = await api("/api/continue");
    // Scope the shelf to the active section (items from unknown/browse-played
    // files count as entertainment).
    const items = (data.items || []).filter(
      (it) => (it.section || "entertainment") === state.section);
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
    const names = st.scope_names || [];
    const label = names.length && names.length <= 3
      ? `Refreshing ${names.join(", ")}… ${st.scanned}/${st.total}`
      : `Scanning drives… ${st.scanned}/${st.total}`;
    bar.textContent = label + (st.added ? ` · +${st.added} new` : "");
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
  // Fresh progress so checkmarks / Resume-course / default module reflect
  // what you just finished watching (not the page-load snapshot).
  await ensureWatchedMap();
  if (rec.type === "show") renderShowDetail(rec);
  else renderMovieDetail(rec);
}

// Season/episode nouns for a record's section ("Module"/"Lesson", ...).
function nomen(rec) {
  const m = SECTION_META[sectionOf(rec)] || {};
  return { season: m.season || "Season", episode: m.episode || "Episode" };
}

function detailHeader(rec) {
  const p = posterMarkup(rec);
  const sec = sectionOf(rec);
  const n = nomen(rec);
  const count = (rec.seasons || []).length;
  let subBits = [];
  if (rec.year) subBits.push(String(rec.year));
  if (rec.type === "show" && count)
    subBits.push(`${count} ${n.season.toLowerCase()}${count === 1 ? "" : "s"}`);
  if (sec === "courses") {
    const prog = courseProgress(rec);
    if (prog > 0) subBits.push(`${Math.round(prog * 100)}% complete`);
  }
  const pill = sec === "entertainment" ? qualityPill(rec) : mediaPill(rec);
  return `
    <div class="detail-hero">
      <div class="detail-poster poster${p.cls}">${pill}${p.html}</div>
      <div class="detail-meta">
        <h1>${escapeHTML(rec.title)}</h1>
        <div class="detail-sub">${escapeHTML(subBits.join(" · "))}</div>
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
    rec.duration_ms || null, false, [], rec.media || null));
  actions.appendChild(play);
}

function renderShowDetail(rec) {
  const n = nomen(rec);
  $("detailBody").innerHTML = detailHeader(rec) + `
    <div class="season-select">
      <label>${escapeHTML(n.season)}</label>
      <select id="seasonSel"></select>
    </div>
    <div id="episodeList" class="episodes"></div>
    <div id="materialsBox"></div>`;

  const actions = $("detailActions");
  const sec = sectionOf(rec);
  const eps = allEpisodes(rec);

  if (sec === "courses") {
    // Resume course: first unwatched lesson, queueing everything after it
    // (across modules) so autoplay carries the whole course.
    const next = eps.find((e) => !(state.progress[e.file_id] || {}).watched);
    const resumeBtn = document.createElement("button");
    resumeBtn.className = "btn primary";
    resumeBtn.textContent = !next ? "Replay course"
      : (next === eps[0] ? "Start course" : "Resume course");
    resumeBtn.addEventListener("click", () => {
      const start = next || eps[0];
      if (!start) { toast("No lessons found."); return; }
      const queue = eps.slice(eps.indexOf(start) + 1).map(queueItem);
      playFile(
        { id: start.file_id, name: start.name, drive_id: rec.drive_id, parent_id: start.parent_id },
        start.duration_ms || null, false, queue, start.media || rec.media);
    });
    actions.appendChild(resumeBtn);
  } else {
    // Shuffle: play every episode in a random order (autoplay carries
    // through the shuffled queue).
    const shuffleBtn = document.createElement("button");
    shuffleBtn.className = "btn";
    shuffleBtn.innerHTML = "⤨ Shuffle";
    shuffleBtn.addEventListener("click", () => {
      const order = shuffle(eps);
      if (!order.length) { toast("No episodes to shuffle."); return; }
      toast(`Shuffling ${order.length} episode${order.length === 1 ? "" : "s"}`);
      const first = order[0];
      const queue = order.slice(1).map(queueItem);
      playFile(
        { id: first.file_id, name: first.name, drive_id: rec.drive_id, parent_id: first.parent_id },
        first.duration_ms || null, true, queue, first.media || rec.media);
    });
    actions.appendChild(shuffleBtn);
  }
  if (state.autoplayNext) {
    const hint = document.createElement("span");
    hint.className = "autoplay-hint";
    hint.textContent = "Autoplay on";
    actions.appendChild(hint);
  }

  const seasons = rec.seasons || [];
  const sel = $("seasonSel");
  seasons.forEach((s, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = s.name
      ? s.name
      : (s.season === 0 ? "Specials" : `${n.season} ${s.season}`);
    sel.appendChild(opt);
  });
  // Open on the season/module of the first unwatched episode (falls back to
  // the first season).
  let startIdx = 0;
  const firstUnwatched = eps.find((e) => !(state.progress[e.file_id] || {}).watched);
  if (firstUnwatched) {
    const idx = seasons.findIndex((s) =>
      (s.episodes || []).some((e) => e.file_id === firstUnwatched.file_id));
    if (idx > 0) startIdx = idx;
  }
  sel.value = String(startIdx);
  sel.addEventListener("change", () => renderEpisodes(rec, seasons[+sel.value]));
  renderEpisodes(rec, seasons[startIdx]);
  renderMaterials(rec);
}

// Course-level workbooks / PDFs, served through the streaming proxy.
function renderMaterials(rec) {
  const box = $("materialsBox");
  if (!box) return;
  const mats = rec.materials || [];
  if (!mats.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<h3 class="materials-head">Materials</h3>` + mats.map((m) =>
    `<a class="material" target="_blank" href="/stream/${encodeURIComponent(m.file_id)}">📄 ${escapeHTML(m.name)}</a>`
  ).join("");
}

// Minimal queue item {file_id, name, duration_ms, media} for the autoplay queue.
function queueItem(ep) {
  return { file_id: ep.file_id, name: ep.name, duration_ms: ep.duration_ms || null,
           media: ep.media || null };
}

// Every playable episode across all seasons, in order.
function allEpisodes(rec) {
  const eps = [];
  for (const s of rec.seasons || [])
    for (const e of s.episodes || []) if (e.file_id) eps.push(e);
  return eps;
}

// Fisher–Yates shuffle (returns a new array).
function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function renderEpisodes(rec, season) {
  const list = $("episodeList");
  list.innerHTML = "";
  if (!season) return;
  const sec = sectionOf(rec);

  // A volume/season ripped as a single audiobook file gets a one-tap play.
  if (season.audiobook && season.audiobook.file_id) {
    const ab = document.createElement("button");
    ab.className = "btn audiobook-btn";
    ab.textContent = `♪ Play ${season.name || "this volume"} as one audiobook`;
    ab.addEventListener("click", () =>
      playFile({ id: season.audiobook.file_id, name: season.audiobook.name,
                 drive_id: rec.drive_id },
               null, false, [], "audio"));
    list.appendChild(ab);
  }

  const eps = season.episodes;
  eps.forEach((ep, idx) => {
    const row = document.createElement("div");
    row.className = "episode";
    const prog = state.progress[ep.file_id] || {};
    if (prog.watched) row.classList.add("watched");
    const num = ep.episode != null
      ? (sec === "entertainment" ? `E${String(ep.episode).padStart(2, "0")}`
                                 : String(ep.episode))
      : "";
    const mark = prog.watched ? `<span class="ep-done">✓</span>`
      : (ep.media === "audio" ? `<span class="ep-audio">♪</span>` : "");
    const pct = !prog.watched && prog.percent > 2
      ? `<div class="progress"><span style="width:${Math.min(98, prog.percent)}%"></span></div>` : "";
    row.innerHTML = `
      <span class="ep-num">${num}</span>
      <span class="ep-title">${escapeHTML(ep.title || ep.name)}</span>
      <span class="ep-dur">${ep.duration_ms ? fmtTime(ep.duration_ms / 1000) : ""}</span>
      <span class="ep-play">${mark || "▶"}</span>${pct}`;
    row.addEventListener("click", () => {
      // Autoplay: queue the rest of this season after the clicked episode.
      const queue = eps.slice(idx + 1).filter((e) => e.file_id).map(queueItem);
      playFile(
        { id: ep.file_id, name: ep.name, drive_id: rec.drive_id, parent_id: ep.parent_id },
        ep.duration_ms || null, false, queue, ep.media || null);
    });
    list.appendChild(row);
  });
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
  const assigned = settings.drive_sections || {};
  state.driveSections = assigned;
  $("autoRefresh").checked = !!settings.auto_refresh_on_startup;
  state.autoplayNext = settings.autoplay_next !== false;
  if ($("autoplayNext")) $("autoplayNext").checked = state.autoplayNext;
  if ($("subtitlesOn")) $("subtitlesOn").checked = settings.subtitles !== false;
  if ($("remoteAccess")) {
    $("remoteAccess").checked = !!settings.remote_access;
    show($("remoteRestartNote"), false);
    renderRemoteBlock();
  }
  const sel = $("playerSelect");
  if (sel) {
    sel.value = settings.player || "auto";
    const avail = settings.available_players || [];
    for (const opt of sel.options) {
      if (opt.value !== "auto" && avail.length && !avail.includes(opt.value)) {
        opt.textContent = opt.textContent.replace(/ \(not installed\)$/, "") + " (not installed)";
      }
    }
    const hint = $("playerHint");
    if (hint && avail.length) hint.textContent =
      "Installed: " + avail.join(", ") + ". mpv and IINA track your position for Continue Watching; VLC now does too via its HTTP interface. mpv stays the recommended default.";
  }
  list.innerHTML = "";
  if (!drives.length) { list.innerHTML = `<div class="empty">No Shared Drives found.</div>`; return; }
  for (const d of drives) {
    const label = document.createElement("label");
    label.className = "drive-row";
    label.innerHTML = `<input type="checkbox" value="${escapeHTML(d.id)}" ${selected.has(d.id) ? "checked" : ""}>
      <span class="drive-name">${escapeHTML(d.name || d.id)}</span>`;
    if (selected.has(d.id)) {
      // Section assignment for included drives.
      const sel = document.createElement("select");
      sel.className = "drive-section";
      sel.dataset.driveId = d.id;
      for (const sec of SECTION_ORDER) {
        const opt = document.createElement("option");
        opt.value = sec;
        opt.textContent = `${SECTION_META[sec].icon} ${SECTION_META[sec].label}`;
        sel.appendChild(opt);
      }
      sel.value = SECTION_META[assigned[d.id]] ? assigned[d.id] : "entertainment";
      label.appendChild(sel);

      const btn = document.createElement("button");
      btn.className = "drive-refresh";
      btn.title = `Refresh only “${d.name || d.id}”`;
      btn.textContent = "⟳";
      btn.addEventListener("click", (e) => {
        e.preventDefault();          // inside a <label>: don't toggle the checkbox
        e.stopPropagation();
        refreshDrives([d.id], d.name || d.id);
      });
      label.appendChild(btn);
    }
    list.appendChild(label);
  }
}

// "Watch on iPhone / iPad" card: show tappable URLs + a QR of the best one.
// URLs come from /api/remote; the QR is rendered server-side at /api/remote/qr.
async function renderRemoteBlock() {
  const on = $("remoteAccess") && $("remoteAccess").checked;
  show($("remoteDetails"), !!on);
  if (!on) return;
  const info = await ensureRemoteInfo(true);
  const urls = (info && info.urls) || [];
  const list = $("remoteUrls");
  list.innerHTML = "";
  if (!urls.length) {
    list.innerHTML = `<p class="muted">Save and restart drivecast — your address appears here once remote access is live.</p>`;
  } else {
    for (const u of urls) {
      const a = document.createElement("a");
      a.className = "remote-url";
      a.href = u.url;
      a.innerHTML = `<span class="remote-url-label">${escapeHTML(u.label)}</span>${escapeHTML(u.url)}`;
      list.appendChild(a);
    }
  }
  const qr = $("remoteQr");
  if (urls.length) {
    qr.onerror = () => show(qr, false);
    qr.src = "/api/remote/qr?_=" + Date.now();
    show(qr, true);
  } else {
    show(qr, false);
  }
}

// Kick a refresh scoped to specific drives (per-drive refresh).
async function refreshDrives(ids, displayName) {
  try {
    const res = await api("/api/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drives: ids }),
    });
    if (res.running && !res.started) toast("A refresh is already running.");
    else toast(`Refreshing ${displayName}…`);
    startScanWatch();
  } catch (e) {
    toast("Refresh failed: " + e.message, "error");
  }
}

async function saveSettings() {
  const selected = [...$("driveList").querySelectorAll("input:checked")].map((c) => c.value);
  const auto = $("autoRefresh").checked;
  const autoplay = $("autoplayNext") ? $("autoplayNext").checked : true;
  // Collect section assignments (entertainment = default, no need to store).
  // Skip drives the user just unchecked — their row still holds a select.
  const driveSections = {};
  for (const sel of $("driveList").querySelectorAll("select.drive-section")) {
    const cb = sel.closest("label").querySelector("input[type=checkbox]");
    if (cb && !cb.checked) continue;
    if (sel.value && sel.value !== "entertainment") driveSections[sel.dataset.driveId] = sel.value;
  }
  $("settingsMsg").textContent = "Saving…";
  try {
    const res = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_drives: selected, drive_sections: driveSections,
        auto_refresh_on_startup: auto, autoplay_next: autoplay,
        subtitles: $("subtitlesOn") ? $("subtitlesOn").checked : true,
        remote_access: $("remoteAccess") ? $("remoteAccess").checked : false,
        player: ($("playerSelect") || {}).value || "auto" }),
    });
    $("settingsMsg").textContent = "Saved.";
    state.selectedDrives = res.selected_drives || [];
    state.driveSections = res.drive_sections || {};
    state.autoplayNext = res.autoplay_next !== false;
    renderTabs();
    // A remote_access flip only takes effect on the next launch.
    if ($("remoteRestartNote")) show($("remoteRestartNote"), !!res.restart_required);
    if (res.restart_required) toast("Restart drivecast to apply remote access.");
    await renderRemoteBlock();
    if (res.refresh_started) { toast("Drives changed — refreshing library…"); startScanWatch(); }
  } catch (e) {
    $("settingsMsg").textContent = "Save failed: " + e.message;
  }
}

// ---------- play ----------
let pendingPlay = null;

async function playFile(f, durationMs, skipResumeCheck, queue, media) {
  const fileId = f.id;
  const name = f.name;
  const durMs = durationMs || null;
  const driveId = f.drive_id || state.driveId;
  const parentId = f.parent_id || state.folderId;
  const q = queue || [];
  const med = media || null;

  let resumeAt = 0;
  if (!skipResumeCheck) {
    try {
      const cont = await api("/api/continue");
      const hit = (cont.items || []).find((x) => x.file_id === fileId);
      if (hit) resumeAt = hit.position || 0;
    } catch (_) {}
  }
  if (resumeAt > 5 && !skipResumeCheck) {
    pendingPlay = { fileId, name, durMs, driveId, parentId, queue: q, media: med, resumeAt };
    $("modalBody").textContent =
      `You were at ${fmtTime(resumeAt)}. Resume or start from the beginning?`;
    show($("modal"), true);
    return;
  }
  await launch(fileId, name, durMs, driveId, parentId, false, q, med, resumeAt);
}

async function launch(fileId, name, durMs, driveId, parentId, startOver, queue, media, resumeAt) {
  // Remote devices never launch mpv on the Mac: play inline when the format is
  // browser-friendly, otherwise offer the VLC/Infuse hand-off.
  if (state.remote) {
    let at = startOver ? 0 : (resumeAt || 0);
    // Continue Watching / course-resume skip the modal (skipResumeCheck) so `at`
    // is unset — locally the server resumes from history, so mirror that here.
    if (!startOver && !at) {
      try {
        const cont = await api("/api/continue");
        const hit = (cont.items || []).find((x) => x.file_id === fileId);
        if (hit) at = hit.position || 0;
      } catch (_) {}
    }
    if (canPlayInBrowser(name)) {
      openWebPlayer({ fileId, name, resumeAt: at, queue: queue || [], media: media || null });
    } else {
      openChooser({ fileId, name, resumeAt: at, queue: queue || [], media: media || null });
    }
    return;
  }
  try {
    const res = await api("/api/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_id: fileId, name, duration_ms: durMs,
        drive_id: driveId, parent_id: parentId, start_over: !!startOver,
        queue: queue || [], media: media || null,
      }),
    });
    const from = res.resumed_from > 1 ? ` (resumed at ${fmtTime(res.resumed_from)})` : "";
    const subs = res.subtitles ? " · EN subs" : "";
    toast(`Playing in ${res.player}${from}${subs}`, "success");
    if (res.player === "vlc") showVlcBanner();
    setTimeout(loadContinue, 1500);
  } catch (e) {
    if (e.status === 501) toast(e.message, "error");
    else toast("Play failed: " + e.message, "error");
  }
}

// Informational only — show briefly and get out of the way (the scan bar
// stays sticky because it reports crucial in-flight work; this doesn't).
function showVlcBanner() {
  const b = $("banner");
  b.innerHTML = "Playing in VLC — resume &amp; Continue Watching are tracked via VLC's " +
    "HTTP interface. If resume doesn't stick, update VLC or install mpv: <code>brew install mpv</code>";
  show(b, true);
  clearTimeout(showVlcBanner._timer);
  showVlcBanner._timer = setTimeout(() => show(b, false), 6000);
}

$("btnResume").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, false, pendingPlay.queue, pendingPlay.media, pendingPlay.resumeAt);
});
$("btnStartOver").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, true, pendingPlay.queue, pendingPlay.media, pendingPlay.resumeAt);
});
$("btnCancel").addEventListener("click", () => { show($("modal"), false); pendingPlay = null; });

// ==================== Web player (iPhone / iPad) ====================
// Fullscreen inline <video> for remote devices. Reports position back to
// /api/progress so Continue Watching / resume sync across devices, and carries
// the autoplay queue client-side (the Mac never sees these files play).
let wp = null; // active web-player session

// Absolute stream URL WITH the token — for external players (VLC/Infuse) that
// don't carry the browser's auth cookie.
function absStreamUrl(fileId) {
  return location.origin + "/stream/" + encodeURIComponent(fileId) +
    "?token=" + encodeURIComponent(state.remoteToken || "");
}

function playInWebPlayer(fileId, name, resumeAt) {
  const video = $("wpVideo");
  $("wpTitle").textContent = name || "";
  video.src = "/stream/" + encodeURIComponent(fileId); // same-origin: cookie auth
  video.currentTime = 0;
  video.load();
  video.play().catch(() => {}); // autoplay may need a tap on iOS — controls cover it
}

function openWebPlayer({ fileId, name, resumeAt, queue, media }) {
  stopProgressTimer();   // a rapid double-tap must not leak the old interval
  wp = { fileId, name, media: media || null, queue: queue || [],
         resumeAt: resumeAt || 0, duration: null, timer: null };
  show($("webPlayer"), true);
  playInWebPlayer(fileId, name, resumeAt || 0);
}

function stopProgressTimer() {
  if (wp && wp.timer) { clearInterval(wp.timer); wp.timer = null; }
}
function startProgressTimer() {
  stopProgressTimer();
  if (wp) wp.timer = setInterval(() => reportProgress(false), 10000);
}

function reportProgress(ended) {
  if (!wp) return;
  const video = $("wpVideo");
  const dur = (video.duration && isFinite(video.duration)) ? video.duration : (wp.duration || null);
  const body = { file_id: wp.fileId, name: wp.name, position: video.currentTime || 0 };
  if (dur) body.duration = dur;
  if (ended) body.ended = true;
  api("/api/progress", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {});
}

function closeWebPlayer() {
  stopProgressTimer();
  if (wp) reportProgress(false); // save final position
  const video = $("wpVideo");
  video.pause();
  video.removeAttribute("src");
  video.load();
  show($("webPlayer"), false);
  wp = null;
  setTimeout(loadContinue, 600);
}

function onWebPlayerEnded() {
  stopProgressTimer();
  reportProgress(true);
  const next = (wp.queue || []).shift();
  if (state.autoplayNext && next && next.file_id && canPlayInBrowser(next.name)) {
    wp.fileId = next.file_id;
    wp.name = next.name;
    wp.media = next.media || null;
    wp.resumeAt = 0;
    wp.duration = null;
    playInWebPlayer(next.file_id, next.name, 0);
  } else {
    closeWebPlayer();
  }
}

(function initWebPlayer() {
  const video = $("wpVideo");
  video.addEventListener("loadedmetadata", () => {
    if (video.duration && isFinite(video.duration)) wp && (wp.duration = video.duration);
    if (wp && wp.resumeAt > 0 && wp.resumeAt < (video.duration || Infinity)) {
      video.currentTime = wp.resumeAt;
    }
  });
  video.addEventListener("play", startProgressTimer);
  video.addEventListener("pause", () => { stopProgressTimer(); reportProgress(false); });
  video.addEventListener("ended", onWebPlayerEnded);
  $("wpClose").addEventListener("click", closeWebPlayer);
})();

// ---------- external-player chooser (MKV etc. on remote devices) ----------
let chooserFile = null;

function openChooser({ fileId, name, resumeAt, queue, media }) {
  chooserFile = { fileId, name, resumeAt: resumeAt || 0,
                  queue: queue || [], media: media || null };
  const enc = encodeURIComponent(absStreamUrl(fileId));
  $("chooserTitle").textContent = name || "";
  $("chooserVlc").href = "vlc-x-callback://x-callback-url/stream?url=" + enc;
  $("chooserInfuse").href = "infuse://x-callback-url/play?url=" + enc;
  show($("chooser"), true);
}

$("chooserBrowser").addEventListener("click", () => {
  show($("chooser"), false);
  if (chooserFile) openWebPlayer({ fileId: chooserFile.fileId, name: chooserFile.name,
    resumeAt: chooserFile.resumeAt, queue: chooserFile.queue, media: chooserFile.media });
});
$("chooserCancel").addEventListener("click", () => { show($("chooser"), false); chooserFile = null; });

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
      onerror="this.parentElement.classList.add('placeholder');
               this.parentElement.insertAdjacentHTML('beforeend', this.dataset.ph||'');this.remove()">`;
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
  const mSection = h.match(/^#\/s\/(\w+)$/);

  if (mSection) {
    setSection(mSection[1]);
    showView("library");
    renderLibrary();
    loadContinue();
    return;
  }
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
if ($("remoteAccess")) $("remoteAccess").addEventListener("change", renderRemoteBlock);

$("filters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip");
  if (!btn) return;
  state.filter = btn.dataset.filter;
  [...$("filters").children].forEach((c) => c.classList.toggle("active", c === btn));
  renderLibrary();
});

$("sortSel").addEventListener("change", async (e) => {
  state.sort = e.target.value;
  try { localStorage.setItem("dc.sort", state.sort); } catch (_) {}
  if (state.sort === "watched") await ensureWatchedMap();
  renderLibrary();
});

$("groupSel").addEventListener("change", async (e) => {
  state.group = e.target.value;
  try { localStorage.setItem("dc.group", state.group); } catch (_) {}
  if (state.group === "drive") await ensureDriveNames();
  renderLibrary();
});

$("search").addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  if (currentRoute() === "library") renderLibrary();
  else location.hash = "#/";
});

window.addEventListener("hashchange", router);

// ---------- init ----------
function restoreControls() {
  try {
    const s = localStorage.getItem("dc.sort");
    const g = localStorage.getItem("dc.group");
    const sec = localStorage.getItem("dc.section");
    if (s) state.sort = s;
    if (g) state.group = g;
    if (sec && SECTION_META[sec]) setSection(sec);
    else setSection(state.section);
  } catch (_) { setSection(state.section); }
  const ss = $("sortSel"), gs = $("groupSel");
  if (ss) ss.value = state.sort;
  if (gs) gs.value = state.group;
}

async function loadPlaybackSettings() {
  try {
    const s = await api("/api/settings");
    state.autoplayNext = s.autoplay_next !== false;
    state.driveSections = s.drive_sections || {};
  } catch (_) { /* non-fatal — defaults to on */ }
}

(async function init() {
  await loadSections();
  restoreControls();
  // On a remote device, load the token up front so the VLC/Infuse deep links
  // (built synchronously when the chooser opens) already have it.
  if (state.remote) await ensureRemoteInfo();
  await loadLibrary();
  await ensureWatchedMap();
  await loadPlaybackSettings();
  if (state.group === "drive") await ensureDriveNames();
  router();
})();
