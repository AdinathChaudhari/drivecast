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
