# Song-DL

A simple web interface for downloading music off of Soulseek.

## About

This is a publicly hosted website for downloading music. It grabs songs from the Soulseek network using Slskd.
Type the name of a song, pick WAV, AIFF, FLAC and/or MP3, and we'll find the best copy of each for you.
Or upload a playlist CSV, see which songs can be found, and download them all as a zip.

Admins can sign in to save songs on the server instead: pick a folder, queue songs or whole playlists, and
leave. The queue keeps downloading, survives restarts, and can be paused, resumed, cancelled and deleted
from the All page.

## Key Differentiators

- Our UI is extremely simple for tech novices. Just a search box and download button.
- Unlike other similar services, we filter out results labeled "PRIVATE".
- Remixes, live takes and other versions rank below the real song unless you ask for them, and copies whose length
  disagrees with the rest are set aside.
- If an uploader is slow, refuses us, or sends a broken file, we quietly move on to the next best copy.

## Pages

| Page | Who | What |
| --- | --- | --- |
| `index.html` | everyone | Search a song or scan a playlist, then download to your browser |
| `login.html` | admins | Sign in (optionally with Cloudflare Turnstile) |
| `server.html` | admins | Same as the home page, but songs are saved on the server in `downloads/<folder>/` |
| `all.html` | admins | Everything queued on the server: pause, resume, cancel, delete |

The web UI lives in `webui/` (see `webui/README.md`), the server in `server/songdl/`.

## Config
An example config file can be found at `.\config.yaml.example`
Copy it to `.\config.yaml` and edit it before starting. You need a Soulseek username and password, and an
admin user and password if you want the Server and All pages.

## Hosting Yourself

1. `git clone https://github.com/SlopHobbyist/song-dl`
2. Configure your `.\config.yaml`
3. `sudo docker compose up -d`

Files end up in `.\downloads`. Files fetched for visitors' browsers are deleted after `max_file_age` days;
songs admins save into folders are kept.

Behind Cloudflare or another reverse proxy, set `webserver.trust_proxy: true` so rate limits and login lockouts
see each visitor's own IP.

## Development

```
python -m venv .venv
.venv/bin/pip install -r server/requirements.txt pytest pytest-aiohttp   # .venv\Scripts\ on Windows
.venv/bin/python -m pytest
```

The tests run the whole server against `server/tests/fake_slskd.py`, a stand-in for slskd. It can also be run
on its own to click through the site without Soulseek:

```
cd server
python -m tests.fake_slskd --downloads ../downloads
# then, in another terminal, with SONGDL_SLSKD_URL=http://127.0.0.1:5030
python -m songdl setup && python -m songdl serve
```

## License
SLSKD is licensed under `GNU Affero GPLv3`.
This repository and all contained code is licensed under `GNU GPLv3`. See .\LICENSE.txt for more info.

## Disclosure

> **Note**: This entire project was written by AI (Claude Code). All code, architecture decisions, and
> implementation details were generated through AI assistance.
