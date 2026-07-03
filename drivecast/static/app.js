// drivecast frontend — vanilla JS, hash routing.
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  drives: [],
  driveName: {},        // id -> name
  view: "home",         // "home" | "browse" | "search"
  driveId: null,
  folderId: null,
  crumbs: [],           // [{name, driveId, folderId}]
  nextPageToken: null,
  loadCtx: null,        // {kind, driveId, folderId, q}
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

function isFolder(f) { return f.mimeType === "application/vnd.google-apps.folder"; }

// ---------- rendering ----------
function show(el, on) { el.classList.toggle("hidden", !on); }

function posterHTML(meta, thumbLink) {
  // Prefer cached TMDB poster, then Drive thumbnail, then gradient placeholder.
  if (meta && meta.poster_key) {
    return `<img loading="lazy" src="/api/poster/${encodeURIComponent(meta.poster_key)}" alt="">`;
  }
  if (thumbLink) {
    const u = `/api/poster/_?thumb=${encodeURIComponent(thumbLink)}`;
    return `<img loading="lazy" src="${u}" alt=""
            onerror="this.parentElement.classList.add('placeholder');this.remove();
                     this.parentElement.insertAdjacentHTML('beforeend', this.dataset.ph||'')">`;
  }
  return "";
}

function placeholderInner(meta, fallbackName) {
  const title = (meta && meta.title) || fallbackName;
  const year = meta && meta.year ? `<div class="ph-year">${meta.year}</div>` : "";
  return `<div class="ph-title">${escapeHTML(title)}</div>${year}`;
}

function escapeHTML(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function videoCard(f, meta) {
  const card = document.createElement("div");
  card.className = "card video";
  const m = meta || {};
  const ph = placeholderInner(m, m.title || f.name);
  let inner = posterHTML(m, f.thumbnailLink);
  const placeholderClass = inner ? "" : " placeholder";
  const badge = m.type === "tv"
    ? `<span class="badge tv">S${String(m.season || 0).padStart(2, "0")}E${String(m.episode || 0).padStart(2, "0")}</span>`
    : "";
  card.innerHTML = `
    <div class="poster${placeholderClass}" data-ph='${escapeHTML(ph)}'>
      ${badge}${inner || ph}
    </div>
    <div class="label">${escapeHTML(m.title || f.name)}</div>
    <div class="sub">${m.year || (f.videoMediaMetadata && f.videoMediaMetadata.durationMillis ? fmtTime(f.videoMediaMetadata.durationMillis / 1000) : "")}</div>`;
  // stash the img fallback template so onerror can inject placeholder text
  const img = card.querySelector("img");
  if (img) img.dataset.ph = ph;
  card.addEventListener("click", () => playFile(f));
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
    location.hash = `#/drive/${state.driveId}/folder/${f.id}`;
  });
  return card;
}

function continueCard(item) {
  const card = document.createElement("div");
  card.className = "card video";
  const pct = Math.max(2, Math.min(98, item.percent || 0));
  card.innerHTML = `
    <div class="poster placeholder" >
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
        location.hash = `#/drive/${d.id}`;
      });
      row.appendChild(c);
    }
  } catch (e) {
    if (e.status === 503) toast("Setup needed: " + e.message, "error");
    else toast("Could not load drives: " + e.message, "error");
  }
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

async function enrichPage(files) {
  const videoNames = files.filter((f) => !isFolder(f)).map((f) => f.name);
  if (!videoNames.length) return {};
  try {
    const data = await api("/api/enrich", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: videoNames }),
    });
    return data.results || {};
  } catch (_) {
    return {};
  }
}

async function renderFiles(files, append) {
  const grid = $("grid");
  if (!append) grid.innerHTML = "";
  const folders = files.filter(isFolder);
  const videos = files.filter((f) => !isFolder(f));
  for (const f of folders) grid.appendChild(folderCard(f));
  // Render videos immediately with placeholders, then enrich.
  const nodes = new Map();
  for (const f of videos) {
    const node = videoCard(f, null);
    nodes.set(f.name, { node, file: f });
    grid.appendChild(node);
  }
  const meta = await enrichPage(files);
  for (const [name, { node, file }] of nodes) {
    const m = meta[name];
    if (m) {
      const fresh = videoCard(file, m);
      node.replaceWith(fresh);
    }
  }
}

async function doBrowse(driveId, folderId, append) {
  state.view = "browse";
  state.driveId = driveId;
  state.folderId = folderId;
  show($("drivesSection"), false);
  show($("continueSection"), false);
  show($("gridSection"), true);
  show($("empty"), false);
  renderBreadcrumb();
  const params = new URLSearchParams({ drive_id: driveId });
  if (folderId) params.set("folder_id", folderId);
  if (append && state.nextPageToken) params.set("page_token", state.nextPageToken);
  if (!append) { $("grid").innerHTML = '<div class="spinner">Loading…</div>'; }
  try {
    const data = await api("/api/browse?" + params.toString());
    if (!append) $("grid").innerHTML = "";
    state.nextPageToken = data.nextPageToken || null;
    $("gridTitle").textContent = state.crumbs.length
      ? state.crumbs[state.crumbs.length - 1].name : (state.driveName[driveId] || "Browse");
    await renderFiles(data.files || [], append);
    show($("loadMore"), !!state.nextPageToken);
    if (!append && !(data.files || []).length) {
      $("empty").textContent = "This folder is empty.";
      show($("empty"), true);
    }
  } catch (e) {
    $("grid").innerHTML = "";
    toast("Browse failed: " + e.message, "error");
  }
}

async function doSearch(q, append) {
  state.view = "search";
  show($("drivesSection"), false);
  show($("continueSection"), false);
  show($("breadcrumb"), false);
  show($("gridSection"), true);
  show($("empty"), false);
  const params = new URLSearchParams({ q });
  if (append && state.nextPageToken) params.set("page_token", state.nextPageToken);
  if (!append) $("grid").innerHTML = '<div class="spinner">Searching…</div>';
  try {
    const data = await api("/api/search?" + params.toString());
    if (!append) $("grid").innerHTML = "";
    state.nextPageToken = data.nextPageToken || null;
    $("gridTitle").textContent = `Results for “${q}”`;
    await renderFiles(data.files || [], append);
    show($("loadMore"), !!state.nextPageToken);
    if (!append && !(data.files || []).length) {
      $("empty").textContent = "No videos found.";
      show($("empty"), true);
    }
  } catch (e) {
    $("grid").innerHTML = "";
    toast("Search failed: " + e.message, "error");
  }
}

// ---------- breadcrumb ----------
function renderBreadcrumb() {
  const bc = $("breadcrumb");
  if (!state.crumbs.length) { show(bc, false); return; }
  bc.innerHTML = "";
  const home = document.createElement("a");
  home.textContent = "Drives";
  home.addEventListener("click", () => { location.hash = "#/"; });
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
        ? `#/drive/${c.driveId}/folder/${c.folderId}` : `#/drive/${c.driveId}`;
    });
    bc.appendChild(a);
  });
  show(bc, true);
}

// ---------- play ----------
let pendingPlay = null;

async function playFile(f, durationMs, skipResumeCheck) {
  const fileId = f.id;
  const name = f.name;
  const durMs = durationMs || (f.videoMediaMetadata && f.videoMediaMetadata.durationMillis) || null;
  const driveId = f.drive_id || state.driveId;
  const parentId = f.parent_id || state.folderId;

  // Check for a saved position to offer resume/start-over.
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

// modal buttons
$("btnResume").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, false);
});
$("btnStartOver").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, true);
});
$("btnCancel").addEventListener("click", () => { show($("modal"), false); pendingPlay = null; });

// ---------- routing ----------
function goHome() {
  state.view = "home";
  state.crumbs = [];
  state.nextPageToken = null;
  show($("drivesSection"), true);
  show($("gridSection"), false);
  show($("breadcrumb"), false);
  show($("empty"), false);
  loadContinue();
}

function router() {
  const h = location.hash || "#/";
  const mBrowseFolder = h.match(/^#\/drive\/([^/]+)\/folder\/([^/]+)$/);
  const mBrowse = h.match(/^#\/drive\/([^/]+)$/);
  const mSearch = h.match(/^#\/search\/(.+)$/);
  state.nextPageToken = null;
  if (mBrowseFolder) {
    doBrowse(decodeURIComponent(mBrowseFolder[1]), decodeURIComponent(mBrowseFolder[2]), false);
  } else if (mBrowse) {
    const id = decodeURIComponent(mBrowse[1]);
    if (!state.crumbs.length) state.crumbs = [{ name: state.driveName[id] || "Drive", driveId: id, folderId: null }];
    doBrowse(id, null, false);
  } else if (mSearch) {
    const q = decodeURIComponent(mSearch[1]);
    if ($("search").value !== q) $("search").value = q;
    doSearch(q, false);
  } else {
    goHome();
  }
}

// ---------- events ----------
$("homeBtn").addEventListener("click", () => { location.hash = "#/"; });

let searchTimer = null;
$("search").addEventListener("input", (e) => {
  const q = e.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (q) location.hash = "#/search/" + encodeURIComponent(q);
    else if (location.hash.startsWith("#/search")) location.hash = "#/";
  }, 300);
});

$("loadMore").addEventListener("click", () => {
  if (state.view === "browse") doBrowse(state.driveId, state.folderId, true);
  else if (state.view === "search") doSearch($("search").value.trim(), true);
});

window.addEventListener("hashchange", router);

// ---------- init ----------
(async function init() {
  await loadDrives();
  router();
})();
