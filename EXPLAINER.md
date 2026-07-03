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

## 6. Browsing, search, and posters

**Browsing** a folder is one call to Google's `files.list` API, asking for
just folders and files whose type starts with `video/` (so your `.aria2`
leftovers, subtitles, and random junk don't clutter the library). Google
returns names, sizes, and — for free — each video's **duration** and a
thumbnail, which drivecast reuses. Folders with thousands of files come back
in pages of 200 with a "Load more" button.

**Search** uses a superpower of the Drive API that would be painfully slow to
build ourselves: one query with `corpora=allDrives` searches **all 40 Shared
Drives at once**, server-side at Google, in under a couple of seconds. (The
rclone-only alternative would have been listing every drive recursively —
minutes, not seconds.)

**Posters** come from three sources, best available wins:

1. **TMDB** (The Movie Database) — *if* you add a free API key to
   `config.json`. drivecast parses each filename with some heuristics —
   strip the `1080p WEB-DL x265`-style junk, pull out the year, spot
   `S01E02` patterns to tell shows from movies — then asks TMDB for the real
   poster and caches it locally.
2. **Google's own thumbnail** for the video, if TMDB is off or finds nothing.
3. A clean **gradient placeholder card** showing the parsed title and year.

Posters and the watch-history JSON are the *only* things drivecast ever
writes to disk (a few MB in `data/`). Video bytes: never.

---

## 7. The one external annoyance: shared rate limits

rclone's built-in Google credentials are shared by every rclone user in the
world, so Google enforces a per-minute query quota on them. If you browse
very fast (or run automated tests), you can briefly hit a **"rate limit
exceeded"** message. drivecast retries with short pauses automatically, and
the quota resets within a minute — but if you ever see that toast, that's
what it is. It's an inconvenience, not a bug, and playback itself (few
requests per second, all lightweight) rarely triggers it.

---

## 8. Map of the code

```
drivecast/
├── app.py                 # entry point: checks rclone works, starts the server, opens browser
├── config.json            # your settings: remote name, port, TMDB key, player preference
└── drivecast/
    ├── rclone_auth.py     # the keymaster: gets fresh tokens out of rclone (section 3)
    ├── drive_api.py       # talks to Google: list drives, browse folders, search
    ├── streaming.py       # the stream proxy / relay (section 4) — the heart
    ├── player.py          # finds mpv/IINA/VLC, launches it, mpv remote-control poller (section 5)
    ├── history.py         # data/history.json: positions, watched flags, Continue Watching
    ├── naming.py          # filename → clean {title, year, season/episode}
    ├── tmdb.py            # optional poster fetching + caching
    ├── server.py          # the web API glueing all of the above together
    └── static/            # the library UI: one HTML page, one JS file, one CSS file
```

Run it with `python app.py`, and your cloud media library is at
`http://127.0.0.1:8737`.
