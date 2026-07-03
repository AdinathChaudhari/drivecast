# drivecast

Stream video straight from your Google **Shared Drives** — no downloads, no
syncing. drivecast is a tiny local web app that browses your Shared Drives,
enriches titles with posters, and plays files in mpv / IINA / VLC while proxying
the bytes on demand. Nothing is ever written to disk.

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
you actually watch are transferred. Video bytes pass through memory in 64 KB
chunks and are never written to disk.

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

### 4. (Optional) TMDB posters

Get a free API key from https://www.themoviedb.org/settings/api and put it in
`config.json` as `tmdb_api_key`. Without a key, drivecast shows clean gradient
placeholder cards (parsed title + year) instead of posters.

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
  isn't configured yet — the server still starts and shows its setup page)
- **Open drivecast** — opens `http://127.0.0.1:8737/` in your browser
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

> The bundled app uses drivecast's built-in defaults (remote `gdrive1`, port
> `8737`). To customise those for the bundled app, edit `config.json` and run
> from source, or rebuild after changing the defaults.

### Run without building (quick test)

You can run the menu-bar app straight from source — this uses your repo's
`config.json`:

```sh
./venv/bin/python drivecast_menubar.py
```

Set `DRIVECAST_NO_BROWSER=1` to skip auto-opening the browser.

## Using the app (everyday flow)

Once `app.py` is running and the library opens in your browser:

1. **Pick a drive.** The home screen shows a **Continue Watching** shelf (empty
   until you've watched something) and a row of all your Shared Drives. Click a
   drive to open it.
2. **Browse.** You'll see that drive's folders and video files. Click a folder
   to go deeper; use the breadcrumb bar at the top to go back up. Big folders
   load 200 items at a time — click **Load more** for the rest.
3. **Play.** Click a video card. Your player (mpv/IINA/VLC) opens and starts
   streaming within a few seconds. Seek anywhere — only the bytes you watch are
   fetched. If you've watched this file before, you'll first be asked
   **Resume from HH:MM / Start over**.
4. **Search.** Type in the search box at the top to find any video across **all**
   your Shared Drives at once (results appear as you type). Click a result to
   play it.
5. **Continue Watching.** With **mpv** (or IINA), drivecast tracks your position
   automatically, so partly-watched titles reappear on the home shelf with a
   progress bar and resume where you left off. With **VLC** playback works fully
   but position isn't tracked (VLC has no simple way to report it back) — that's
   the only reason the app nudges you toward `brew install mpv`.

That's the whole loop: **run `app.py` → browse or search → click to play.** Leave
the terminal running in the background while you watch.

### Turning on posters (TMDB)

By default, cards show a clean gradient placeholder with the parsed title and
year. To get real movie/TV posters:

1. Get a free API key: https://www.themoviedb.org/settings/api (Developer plan,
   no fee). For the sign-up form, "Application URL" can be
   `http://localhost:8737` and "Type of Use" is **Desktop Application**.
2. Open `config.json` and set `"tmdb_api_key": "your-key-here"`.
3. Restart `app.py`. Posters are fetched on demand and cached in
   `data/posters/`, so they only download once.

## Configuration (`config.json`)

Auto-created from `config.example.json` on first run.

| key            | default   | meaning                                             |
|----------------|-----------|-----------------------------------------------------|
| `remote`       | `gdrive1` | rclone remote name (no trailing colon)              |
| `tmdb_api_key` | `""`      | optional TMDB v3 API key for posters                |
| `player`       | `auto`    | `auto` \| `mpv` \| `iina` \| `vlc`                  |
| `port`         | `8737`    | local port (bound to 127.0.0.1 only)                |
| `page_size`    | `200`     | Drive files.list page size                          |

## Data (runtime, gitignored)

- `data/history.json` — resume positions & watched state, keyed by Drive file id
- `data/tmdb_cache.json` — cached TMDB lookups (including negative results)
- `data/posters/` — downloaded w342 posters

## Notes

- The server binds to `127.0.0.1` only — it is not exposed on your network.
- Operations are keyed by Drive **file id**, so filenames with quotes/unicode
  are handled safely.
- Shared-drive queries always set `supportsAllDrives` / `includeItemsFromAllDrives`.
