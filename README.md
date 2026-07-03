# drivecast

Stream video straight from your Google **Shared Drives** — no downloads, no
syncing. drivecast is a tiny local web app that presents your Shared Drives as a
**cached media library** — movie and TV-show tiles with pre-fetched posters,
seasons and episodes — and plays files in mpv / IINA / VLC while proxying the
bytes on demand. Nothing is ever written to disk.

**Library model.** You pick which Shared Drives to include (Settings, or the
menu-bar app). drivecast scans those drives **once** and caches a structured
catalogue to `library.json`. From then on, normal browsing is instant and
hits the Google API **zero times** — tiles, seasons, episodes and posters all
come off disk. A **Refresh** (manual or on launch) rescans and diffs: new titles
are added (and their posters fetched), deleted titles are removed (and their
orphaned posters pruned), and show episode lists are updated. The raw
folder-browser is still available behind a demoted **Browse files** link.

**Collection folders.** The scan recurses *into* folder trees, so a collection
folder (`Phase 1`, `Hollywood`, `Blade Series`, `The Godfather Series`, …) that
holds many films — as loose files or one-movie subfolders, possibly nested —
surfaces **each film as its own tile** rather than one wrong tile named after the
folder. Bonus-material subfolders (`Featurettes`, `Extras`, `Behind the Scenes`,
…) are ignored, and a leading enumeration prefix (`01) `, `01.`, `1 - `) is
stripped from titles. TV shows (season subfolders or episode-marked files) are
still detected and kept as a single show tile.

> drivecast is strictly **read-only** on Google Drive — it never deletes, trashes
> or moves anything. Only the local cache (posters/temp) is written.

## How streaming works (no downloads)

drivecast never copies a file locally. When you press play it launches your
video player pointed at a local URL:

```
http://127.0.0.1:8737/stream/<file_id>
```

That endpoint is a **Range-aware proxy**. It forwards the player's `Range`
header verbatim to the Google Drive media API
(`files/{id}?alt=media`) and streams the response straight back — a 206 Partial
Content with the correct `Content-Range`, so seeking works and only the bytes
you actually watch are transferred. Video bytes pass through memory in 1 MiB
chunks and are never written to disk. The proxy reuses a single long-lived,
connection-pooled HTTP client (HTTP/2 when the optional `h2` package is present),
so a player's many seek/Range requests avoid per-request TCP+TLS setup.

rclone is used purely as the **token authority**: `rclone backend drives
gdrive1:` refreshes the OAuth token as a side effect, and drivecast reads the
fresh `access_token` from `rclone config dump`. All browsing, searching and
streaming go directly against the Google Drive v3 API with that Bearer token.

## Setup

### 1. Configure an rclone Google Drive remote (assumed already done)

drivecast expects a working rclone remote with Google Drive **full** scope,
named `gdrive1` by default:

```sh
brew install rclone
rclone config          # new remote -> type "drive" -> full access -> authorise
rclone backend drives gdrive1:   # should print your Shared Drives as JSON
```

The rclone config must **not** be encrypted — drivecast reads the token
non-interactively.

### 2. Install drivecast dependencies

```sh
cd drivecast
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3. (Optional) Install mpv for resume tracking

```sh
brew install mpv
```

mpv (and IINA) expose a JSON IPC socket that drivecast polls to save your
playback position, powering the **Continue Watching** shelf and resume prompts.
VLC works too but has no resume tracking — you'll see a banner suggesting mpv.

**Choosing a player.** By default drivecast auto-picks mpv → IINA → VLC. To force
one, use **Settings → Video player** in the app (it shows which players are
installed), or set `"player"` in `config.json` to `auto` / `mpv` / `iina` / `vlc`.
Playback works the same in any of them (VLC streams the local URL by requesting
byte-ranges, just like mpv); only mpv/IINA report your position back for resume.

### 4. (Optional) TMDB posters

Get a free API key from https://www.themoviedb.org/settings/api and put it in
**`~/Library/Application Support/drivecast/secrets/secrets.json`** (see
[Secrets & security](#secrets--security)) as `{"tmdb_api_key": "…"}`. Without a
key, drivecast shows clean gradient placeholder cards (parsed title + year)
instead of posters.

## Run

```sh
./venv/bin/python app.py
```

This starts the server on `http://127.0.0.1:8737/` and opens your browser. Set
`DRIVECAST_NO_BROWSER=1` to skip opening the browser.

If rclone can't produce a token, the web UI shows a friendly setup page instead
of the library.

To **stop** the app, press `Ctrl+C` in the terminal where it's running (or just
close the terminal). Your player keeps playing independently — drivecast is only
needed to start playback and feed the stream, so quitting it stops new plays but
doesn't kill a movie already open in mpv/VLC.

## Menu-bar app / building `drivecast.app`

Prefer no terminal? drivecast ships a native macOS **menu-bar app**. It runs the
same server in-process and puts a small **☁** icon in your menu bar:

- a status line — `drivecast: running on :8737` (or `setup needed` if rclone
  isn't configured yet, or `scanning… n/total` while a refresh runs)
- **Open drivecast** — opens `http://127.0.0.1:8737/` in your browser
- **Drives to include** — a submenu listing every Shared Drive as a checkable
  item; toggling one updates `selected_drives` and kicks a background refresh
- **Refresh library** — rescans the selected drives now
- **Auto-refresh on launch** — checkable toggle (`auto_refresh_on_startup`)
- **Quit** — cleanly shuts the server down and exits

If drivecast is already running when you launch it again, it just opens the
existing instance in your browser and exits (it won't start a second server).

### Requirements

rclone must be set up on the machine (see **Setup** above) — the app reads the
Drive token from your rclone config exactly like `app.py` does. A player
(mpv / IINA / VLC) is needed to actually play video; mpv is recommended for
resume tracking (`brew install mpv`).

### Build the bundle

```sh
cd drivecast
./venv/bin/pip install rumps py2app        # runtime + build deps
./venv/bin/python setup_app.py py2app
```

The bundle is written to **`dist/drivecast.app`**.

### Install & launch

- Drag **`dist/drivecast.app`** to **/Applications**.
- Launch it from **Spotlight** (⌘-Space → "drivecast") or the Applications
  folder / Dock. It's a menu-bar agent (`LSUIElement`), so it shows a ☁ in the
  menu bar rather than a Dock icon.
- **Quit** from the menu-bar dropdown → **Quit**.

> The bundled app and the source app share the same settings, secrets and
> library in `~/Library/Application Support/drivecast/`, so your drive selection,
> TMDB key, watch history and posters **persist across rebuilds**. Change most
> settings in-app (**Settings** / the menu-bar submenus); edit
> `~/Library/Application Support/drivecast/config.json` directly for the rest.

### Run without building (quick test)

You can run the menu-bar app straight from source — this uses your repo's
`config.json`:

```sh
./venv/bin/python drivecast_menubar.py
```

Set `DRIVECAST_NO_BROWSER=1` to skip auto-opening the browser.

## Using the app (everyday flow)

Once `app.py` is running and the library opens in your browser:

1. **Pick your drives (first run).** If you haven't selected any drives yet, the
   home screen prompts you to open **Settings**, where every Shared Drive is
   listed as a checkbox. Tick the ones you want, optionally enable
   **Auto-refresh on launch**, and **Save**. Saving kicks off the first scan.
   (You can also do this from the menu-bar app's **Drives to include** submenu.)
2. **Wait for the scan.** A progress bar shows `Scanning drives… n/total` while
   drivecast walks the selected drives and builds the library. This is the only
   time normal use touches the Google API; it's throttled and retries on rate
   limits, so it's safe to leave running.
3. **Browse the library.** The home screen shows a **Continue Watching** shelf on
   top, then your library as a grid of movie/show tiles with posters. Filter by
   **All / Movies / TV Shows**, and use the search box for an instant,
   offline search over the cached library.
4. **Open a title.** Click a movie tile for its detail page (poster, overview,
   **Play**). Click a show tile for its detail page with a **season selector** and
   that season's episodes in order — click an episode to play it.
5. **Play.** Your player (mpv/IINA/VLC) opens and starts streaming within a
   couple of seconds (duration/size come from the cache, so there's no blocking
   metadata call). Seek anywhere — only the bytes you watch are fetched. If you've
   watched this file before, you'll first be asked **Resume / Start over**.
6. **Continue Watching.** With **mpv** (or IINA), drivecast tracks your position
   automatically, so partly-watched titles (including the right *episode* of a
   show) reappear on the home shelf and resume where you left off. With **VLC**
   playback works fully but position isn't tracked — that's the only reason the
   app nudges you toward `brew install mpv`.
7. **Refresh.** When you add or remove content on the drives, click **Refresh**
   (top bar) or the menu-bar **Refresh library** item. drivecast rescans, adds
   new titles, removes deleted ones, updates show episode lists, and backfills
   posters for any title still missing one (so enabling a TMDB key and hitting
   Refresh gives every existing tile a poster) — all without disturbing what
   you're watching.
8. **Browse raw files (advanced).** The **Browse files** link still gives you the
   old live folder-by-folder browser over any drive, if you ever need it.

### Turning on posters (TMDB)

Posters are **pre-cached during the scan**: for each title without one drivecast
resolves TMDB (movie vs TV, by title + year), downloads the w342 poster to the
posters cache, and stores its path in the library record — so tiles load
instantly from disk with no per-card lookup. Without a key (or when there's no
match) a tile falls back to a clean gradient placeholder with the title and year.

To enable posters:

1. Get a free API key: https://www.themoviedb.org/settings/api (Developer plan,
   no fee). For the sign-up form, "Application URL" can be
   `http://localhost:8737` and "Type of Use" is **Desktop Application**.
2. Put the key in `~/Library/Application Support/drivecast/secrets/secrets.json`
   (see [Secrets & security](#secrets--security)).
3. Restart `app.py` and **Refresh** the library. The scan backfills posters for
   **every** title still missing one, so a first Refresh after adding the key
   fills in your whole existing library, not just newly-added titles.

## Secrets & security

drivecast is designed so **nothing personal ever reaches the repo**. All private
material lives outside the repo (in `~/Library/Application Support/drivecast/`)
and is loaded at runtime:

- **API keys** → `~/Library/Application Support/drivecast/secrets/secrets.json`:

  ```json
  { "tmdb_api_key": "your-tmdb-key" }
  ```
  (The repo ships `secrets/secrets.example.json` only as a format reference.)

  You can also pass it via the `DRIVECAST_TMDB_API_KEY` environment variable.
  Keys are **never** written back into `config.json`, so they can't leak there.
- **Google Drive credentials** never touch drivecast — they live only in your
  rclone config.
- **Config, data and secrets live in a stable per-user directory**,
  `~/Library/Application Support/drivecast/` (`config.json`, `data/`,
  `secrets/secrets.json`) — *not* inside the repo or the packaged `.app`. This
  means rebuilding/reinstalling the app never wipes your selected drives, library
  or history, and the bundled app can read your TMDB key. It all stays local and
  never reaches git. (`config.example.json` in the repo is only the first-run
  template.)
- The web server binds to **`127.0.0.1` only** — never exposed on your network.
- A **pre-commit hook** (`scripts/install-hooks.sh`) refuses to commit secret
  files or key-shaped strings as a backstop. Install it after cloning:

  ```sh
  scripts/install-hooks.sh
  ```

The only thing you must supply is the rclone remote and (optionally) a TMDB key;
neither is stored anywhere git can see.

## Configuration (`config.json`)

Lives at `~/Library/Application Support/drivecast/config.json`, auto-created from
the repo's `config.example.json` on first run. Holds **non-secret** settings
only — secrets go in `secrets/` (above).

| key            | default   | meaning                                             |
|----------------|-----------|-----------------------------------------------------|
| `remote`       | `gdrive1` | rclone remote name (no trailing colon)              |
| `player`       | `auto`    | `auto` \| `mpv` \| `iina` \| `vlc`                  |
| `port`         | `8737`    | local port (bound to 127.0.0.1 only)                |
| `page_size`    | `200`     | Drive files.list page size                          |
| `selected_drives` | `[]`   | Shared Drive ids included in the library            |
| `auto_refresh_on_startup` | `false` | rescan the library on each launch        |
| `scan_throttle` | `0.15`   | seconds to pause between scan API calls (quota)     |

`selected_drives` and `auto_refresh_on_startup` are normally set from the
**Settings** view or the menu-bar app rather than by hand. (`tmdb_api_key` is a
secret — set it in `secrets/secrets.json`, not here.)

## Data (runtime, local)

All under `~/Library/Application Support/drivecast/data/` (persists across app
rebuilds):

- `library.json` — the cached catalogue (movie/show records, seasons,
  episodes, poster paths); rebuilt by a scan/refresh
- `history.json` — resume positions & watched state, keyed by Drive file id
- `tmdb_cache.json` — cached TMDB lookups (including negative results)
- `posters/` — downloaded w342 posters

## Notes

- The server binds to `127.0.0.1` only — it is not exposed on your network.
- Operations are keyed by Drive **file id**, so filenames with quotes/unicode
  are handled safely.
- Shared-drive queries always set `supportsAllDrives` / `includeItemsFromAllDrives`.
- **Rate limits / quota.** The library cache is designed so normal browsing hits
  the Google API zero times; only a scan/refresh does, and it backs off and
  retries on `rateLimitExceeded` / 429 instead of crashing. If you share rclone's
  built-in OAuth client with many users you'll still hit its tiny per-minute
  quota during large scans. The real fix is to use **your own** Google OAuth
  client id/secret in the rclone remote (a free Google Cloud project) — that
  gives you your own generous quota. Set that up separately in `rclone config`.
