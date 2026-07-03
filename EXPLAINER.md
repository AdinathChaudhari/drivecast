# drivecast, Explained From the Ground Up

*A layer-by-layer walkthrough of what was built, why it was built that way, and
how every piece works — written for someone with a basic understanding of code.*

---

## 1. The problem we started with

Thanks to `drive-offload`, your movies and shows now live on **Google Shared
Drives** instead of your nearly-full Mac. That solved storage — but created a
viewing problem. To watch anything, you'd have to download it back first,
which defeats the whole point of pushing it to the cloud.

So the wish was:

> "Give me something like Infuse or VLC — browse my Shared Drives like a
> media library and press play — but it must **stream** the video, never
> download it."

drivecast is that something: a small web app that runs on your Mac. You open
it in a browser, see all 40 of your Shared Drives, browse folders as a
poster grid, and click a video. A real video player opens and starts playing
within seconds — even if the file is 20 GB — and you can jump to any point in
the movie instantly. Nothing lands on your disk.

---

## 2. The key idea: watching *is* downloading — just only the part you watch

Here's the mental shift that makes everything click: **there is no magical
difference between "streaming" and "downloading."** Netflix downloads video
to your device too — it just downloads *only the few seconds you're about to
watch*, plays them, throws them away, and asks for the next few seconds.

The mechanism behind this is a standard HTTP feature called a **Range
request**. A normal download says "give me the file." A Range request says
"give me **bytes 5,000,000 through 6,000,000** of the file." Google Drive's
API happily answers Range requests.

That single feature gives us everything a player needs:

- **Instant start** — the player asks for the first chunk and starts playing
  it while requesting the next one. No waiting for 20 GB.
- **Instant seeking** — jump to the middle of the movie and the player simply
  asks for bytes starting at the middle. Google serves them in a second or
  two, no matter how big the file is.
- **Zero disk usage** — the chunks flow from Google, through drivecast, into
  the player's memory, onto your screen, and are gone. The stream path in the
  code contains no file writes at all — it *can't* download to disk even by
  accident.

Video players already know how to do all of this. mpv and VLC constantly make
Range requests when playing anything over the network. So drivecast doesn't
implement a player — it implements the thing players need on the other end:
a URL that answers Range requests with your Drive's bytes.

---

## 3. The keymaster: rclone (again)

Just like drive-offload, drivecast never asks you to log into Google or set
up developer credentials. It borrows from the work you already did: your
rclone remote **`gdrive1:`**, which holds a valid Google login.

Google's API doesn't accept "I'm rclone, trust me" — every request needs an
**access token**, a temporary password string that expires after about an
hour. rclone stores its current token in its config file and knows how to
refresh it when it goes stale. drivecast piggybacks on that:

1. It runs `rclone backend drives gdrive1:` — ostensibly to list your Shared
   Drives, but with a useful side effect: rclone notices if its token is
   stale, refreshes it with Google, and saves the fresh one.
2. It then runs `rclone config dump` and reads the fresh token out of the
   config.

So rclone remains the one and only keeper of your Google login; drivecast
just asks it for the current key whenever needed. (This is the exact trick
`todrive` in drive-offload already used — proven to work on your machine.)

The token expiring every hour sounds like a problem for a 3-hour movie, but
it isn't — and the reason is elegant. Because playback is thousands of small
Range requests rather than one giant download, **each request gets a fresh
token stamped on it** as it passes through drivecast. Google checks the token
at the *start* of each request. So even if the token expires mid-movie, the
very next chunk request simply carries the new one. Expiry can never
interrupt playback.

---

## 4. The architecture: three players and a middleman

```
┌───────────┐        ┌────────────────────┐        ┌──────────────┐
│  Browser   │  API   │     drivecast      │ Range  │ Google Drive │
│ (library   │◄──────►│  (Python web app   │◄──────►│     API      │
│  UI)       │        │   on 127.0.0.1)    │ chunks │              │
└───────────┘        └─────────┬──────────┘        └──────────────┘
                               │ launches, then feeds
                               ▼
                      ┌────────────────┐
                      │  mpv / VLC     │  ◄── plays http://127.0.0.1:8737/stream/<file-id>
                      └────────────────┘
```

- **The browser** shows the library: drive list, folders, posters, search,
  Continue Watching. It never touches video — it only talks to drivecast's
  small JSON API ("what's in this folder?", "play this file").
- **drivecast** is a Python web server (FastAPI) running only on your own
  machine (`127.0.0.1` — deliberately unreachable from the network, because
  it's effectively a proxy into your entire Drive).
- **The video player** (mpv or VLC) is given a plain local URL like
  `http://127.0.0.1:8737/stream/abc123`. As far as the player knows, it's
  playing an ordinary web video. It has no idea Google Drive exists.

### The stream proxy — the heart of the app

When the player asks drivecast for `bytes 5,000,000–6,000,000` of file
`abc123`, drivecast:

1. grabs a fresh access token from the rclone trick above,
2. forwards the *exact same* Range request to Google's
   `files/abc123?alt=media` endpoint with the token attached,
3. pipes Google's answer straight back to the player, 64 KB at a time,
   never touching disk.

It's a relay — a translator that adds the secret handshake (the token) that
the player doesn't know.

One funny-looking behavior is actually normal: every time you **seek**, the
player rudely hangs up its current connection and opens a new one at the new
position. The server logs show a burst of aborted requests. Early versions of
software like this often treat those as errors; drivecast expects them and
quietly closes the matching connection to Google.

---

## 5. What happens when you click Play (the whole story)

1. You click a poster in the browser. If you've watched part of it before, a
   dialog asks **"Resume from 42:10, or start over?"**
2. The browser tells drivecast: "play file `abc123`, resume at 2530 seconds."
3. drivecast looks for a player on your Mac, in order of preference:
   **mpv** (best) → **IINA** (a Mac app built on mpv) → **VLC**. Today your
   Mac has VLC, so VLC opens. (The UI shows a gentle banner suggesting
   `brew install mpv` — read on for why.)
4. The player is launched with the local stream URL and told to start at
   42:10. It makes its first Range request, drivecast relays it, and the
   movie appears within a few seconds.
5. If the player is **mpv**, drivecast also opens a tiny side-channel: mpv
   can expose a "remote control socket" where you can ask it questions. A
   background thread asks *"what's the current playback time?"* every 3
   seconds and writes the answer into a small history file. That's how
   resume positions and the Continue Watching shelf stay accurate to within
   ~3 seconds — even if you force-quit the player.
6. When you quit, the final position is saved. If you were past 90 % of the
   movie, it's marked **watched** and drops off Continue Watching.

VLC, unfortunately, has no comparably simple side-channel — so with VLC you
get full streaming and seeking, but drivecast can't *see* where you stopped.
That's the one real reason the app nudges you toward mpv.

---

## 6. The library cache (and browsing, search, posters)

Early drivecast was a live folder browser: every click was a Google API call.
That was slow and, on rclone's shared credentials, quick to hit the rate limit.
The current design flips it around: drivecast keeps a **cached library** and
serves browsing entirely from disk.

**Selecting drives.** You choose which Shared Drives to include (Settings view or
the menu-bar **Drives to include** submenu). Only those drives appear; the rest
are invisible. The choice lives in `config.json` as `selected_drives`.

**The scan.** A scan (first run, on launch if you enabled auto-refresh, or when
you hit **Refresh**) walks each selected drive's folder tree via `files.list`,
building a **nested tree** that preserves the folder hierarchy, and then
classifies each folder **recursively**:

- A folder is a **TV show** if it has a direct season-named subfolder (`Season 1`,
  `S01`, `Series 2`, …) **or** ≥2 of its videos (directly, or in its immediate
  season subfolders) carry episode markers (`S01E02`, `2x05`, `Episode 3`, `E04`).
  A show becomes **one tile**: its descendant videos are grouped into seasons
  (from the subfolder name, else the `SxxExx` marker, else season 1) and sorted by
  episode number.
- Otherwise the folder **expands into movies**, recursing down the tree so each
  film is its own tile:
  - A **leaf movie folder** with one main video becomes one movie titled from the
    *folder* name; with several main videos, one movie per video, titled from each
    *file* name.
  - A **container** (a collection like `Phase 1`, `Hollywood`, `Blade Series`,
    `The Godfather Series`, holding movie subfolders and/or loose movie files,
    possibly nested) recurses into each subfolder and turns any stray loose video
    into its own tile — so a `Phase 1` folder yields *Iron Man*, *The Incredible
    Hulk*, *Thor*, … rather than one bogus `Phase 1` tile.
  - **Bonus-material subfolders** (`Featurettes`, `Extras`, `Bonus`, `Behind the
    Scenes`, `Deleted Scenes`, `Sample(s)`, `Subs`, `Subtitles`, `Trailers`) are
    skipped, and a leading **enumeration prefix** (`01) `, `01.`, `1 - `) is
    stripped from titles (without touching real leading numbers like *2 Fast 2
    Furious* or *1917*).
- Loose videos sitting at a drive's root are parsed by filename: `SxxExx` files
  group into a show; everything else is a standalone movie.

Each movie record is keyed by its Drive **file id** (unique and stable), so the
same film never collides with another tile.

**Grouping seasons into one show.** Real drives store seasons in wildly different
ways, so after the first pass drivecast merges them so each show is a single tile:

- `The Office/Season 1/…` — seasons nested under a show folder (already one show).
- `Blackadder Season 1 S01`, `Blackadder Season 2 S02`, … — separate top-level
  folders sharing a name prefix are grouped under that prefix (`Blackadder`).
- `Season 1`, `Season 2`, … as bare folders — the **whole drive** is the show, so
  they're grouped under the drive's name (`Fraiser`).
- A show split across `… (Part 1)` / `(Part 2)` drives merges into one show.

Quality noise in folder names (`Season 1 (480p DVD)`,
`Blackadder (1983) S01 (576p x265 …)`) is stripped before detection, and a
whole-series wrapper named as a range (`Season 1-9 S01-s09`) is left as-is.

The result is written to **`data/library.json`** (atomically) as structured
records — title, year, type, and for shows the full season/episode tree, with
each file's size and **duration** pulled straight from the list response so
playback later needs no extra metadata call. From then on the home grid, detail
pages, seasons and episodes all render from this file: **zero API calls**.

**Refresh diffing.** A refresh rescans and diffs against the existing library:
newly-found titles are added, titles whose files are gone are removed (and their
now-orphaned posters deleted), and show episode lists are updated in place. A
`/api/refresh/status` endpoint drives the progress bar.

**Posters, pre-cached.** During the scan, every title that doesn't already have a
poster is resolved against **TMDB** (movie vs TV, by title+year, if you set an API
key), its w342 poster is downloaded to the posters cache, and the local path is
stored in the record — so tiles load instantly from disk with no per-card lookup.
Because the scan backfills *every* poster-less title (not just newly-added ones),
adding a TMDB key and hitting Refresh fills in your whole existing library.
Negative results are cached too. No key or no match → a clean **gradient
placeholder** with the title and year. TMDB is purely additive.

**Search** is now instant and offline: the home search box filters the cached
library client-side. The old server-side `corpora=allDrives` search (one query
across all drives at Google) still backs the demoted **Browse files** view, along
with the original live folder browser.

`library.json`, the posters, and the watch-history JSON are the *only* things
drivecast writes to disk (a few MB in `data/`). Video bytes: never.

---

## 7. The one external annoyance: shared rate limits

rclone's built-in Google credentials are shared by every rclone user in the
world, so Google enforces a tiny per-minute query quota on them. Because normal
browsing now serves from `data/library.json`, day-to-day use hits the API **zero
times**; only a scan/refresh talks to Google. That scan is built to survive the
quota: it throttles between calls and, on a `rateLimitExceeded` / 429 response,
**backs off exponentially and retries** rather than crashing — and if a single
folder keeps failing it's skipped so the rest of the scan still completes.

If you routinely scan large drives on the shared credentials you'll still hit the
limit eventually. The real fix is to put **your own** Google OAuth client
id/secret into the rclone remote (a free Google Cloud project), which gives you
your own generous quota. That's configured separately in `rclone config`.

---

## 8. Map of the code

```
drivecast/
├── app.py                 # entry point: checks rclone works, starts the server, opens browser
└── drivecast/
    ├── config.py         # config/data/secrets under ~/Library/Application Support/drivecast (persists across rebuilds)
    ├── rclone_auth.py     # the keymaster: gets fresh tokens out of rclone (section 3)
    ├── drive_api.py       # talks to Google: list drives, browse, search — with rate-limit backoff
    ├── library.py         # the cache: recursive scan/classify drives → library.json, diff, posters (section 6)
    ├── streaming.py       # the stream proxy / relay (section 4) — the heart
    ├── player.py          # finds mpv/IINA/VLC, launches it (with cache/hwdec flags), mpv poller (section 5)
    ├── history.py         # history.json: positions, watched flags, Continue Watching
    ├── naming.py          # filename/folder → clean {title, year, season/episode}; enum-prefix + extras helpers
    ├── tmdb.py            # poster fetching + caching (pre-cached/backfilled during the scan)
    ├── server.py          # the web API: /api/library, /api/title, /api/refresh, /api/settings, /stream, …
    └── static/            # the library UI: one HTML page, one JS file, one CSS file

Config, data and secrets live outside the tree, in
~/Library/Application Support/drivecast/ (config.json, data/, secrets/secrets.json),
so they survive app rebuilds and the packaged .app can read them.
```

Run it with `python app.py`, and your cloud media library is at
`http://127.0.0.1:8737`.

---

## 9. How you actually use it, day to day

The whole thing boils down to three steps: **start it, browse or search, click
to play.**

**Starting it.** Open a terminal, go to the drivecast folder, and run:

```
./venv/bin/python app.py
```

The terminal prints a couple of status lines (that rclone is OK, and which
player it found), then your browser opens to the library. Leave that terminal
window running in the background the entire time you're watching — drivecast is
the middleman feeding video to the player, so it needs to stay alive. When
you're done for the day, click back to that terminal and press **Ctrl+C** to
stop it. (A movie already open in VLC/mpv keeps playing; you just can't start
new ones once drivecast is stopped.)

**The library.** The first thing you see is a home screen with two things: a
**Continue Watching** shelf across the top (empty at first — it fills in as you
watch), and a row of all 40 of your Shared Drives. It looks like Netflix or
Infuse, not a file browser.

**Watching something.**

1. Click a Shared Drive → you see its folders and videos as cards.
2. Click into folders to drill down; the breadcrumb bar at the top takes you
   back. (A folder with thousands of files shows the first 200 with a "Load
   more" button.)
3. Click a video → your player opens and the movie starts in a few seconds.
   Jump to any point and it re-buffers from there almost instantly.
4. If you've watched part of it before, drivecast first asks **"Resume from
   42:10, or start over?"**

**Finding something fast.** Instead of browsing, type a title into the search
box at the top. drivecast asks Google to search *all* your drives at once, so a
film buried three folders deep in some drive you forgot about shows up in a
couple of seconds. Click the result to play.

**Continue Watching** only truly works if your player is **mpv** (or IINA),
because those let drivecast peek at your current position every few seconds. If
you're on **VLC**, everything plays and seeks perfectly — you just won't get the
resume shelf, because VLC won't tell drivecast where you stopped. That single
limitation is the only reason the app suggests `brew install mpv`.

**Posters.** Out of the box you'll see tidy placeholder cards (title + year on a
gradient). If you drop a free **TMDB API key** into `secrets/secrets.json` (see
the README), the cards turn into real movie and show posters — fetched once and
cached, so it's fast forever after. This is purely cosmetic; the app works
identically without it.

That's it. There's no library to import, no scan to wait for, nothing syncs. You
run one command and your entire cloud collection is right there to play.

---

## 10. Keeping your data private (why this repo is safe to open-source)

drivecast is deliberately built so the *code* is shareable but *your stuff* never
is. The split is clean:

- **Secrets, config and data live outside the repo**, in a stable per-user
  directory: `~/Library/Application Support/drivecast/`. Your TMDB key sits in
  `secrets/secrets.json` there (or the `DRIVECAST_TMDB_API_KEY` environment
  variable), and it's — importantly — **never written back into `config.json`**,
  so it can't accidentally end up in a committed file. Keeping everything in this
  stable location also means it **persists across app rebuilds** (a py2app rebuild
  no longer wipes your selected drives / library / history) and the packaged
  `.app` can read your key. Only `config.example.json` — a blank template — ships
  in the repo.
- **Your Google login** isn't in drivecast at all. It lives in rclone's own
  config; drivecast only ever borrows a short-lived token at runtime.
- **Your library** — the drive and file names, watch history, posters — all sit
  in `~/Library/Application Support/drivecast/data/`, outside the repo. None of it
  is in git.
- The server only ever listens on `127.0.0.1`, so it's not reachable from the
  network even while running.
- As a backstop, a **pre-commit hook** scans staged changes and refuses to commit
  anything that looks like a key, token, or credential file — so even a slip of
  `git add` can't leak a secret.

The result: the repository contains only the program. Clone it, add your own
rclone remote and (optionally) your own TMDB key, and it's yours — with none of
the original author's data anywhere in it.
