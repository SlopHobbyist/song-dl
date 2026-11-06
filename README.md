> Last tested working 11/06/2026

# Song-DL

A simple web interface for downloading music off of Soulseek, SoundCloud, or YouTube.

## About

This is a website for downloading music. It grabs songs from the Soulseek network using [slskd](https://github.com/slskd/slskd). Type a song name, and it'll find the highest quality version available. If it's not on Soulseek, it'll try SoundCloud then YouTube. You can also search by pasting playlist links, or uploading txt/csv files. If you have a NAS, admins can choose to download to the server instead.

## Differentiators

- Easy for noobs, simply search then download
- We filter out crap like files labeled "PRIVATE", and remixes+live versions when you didn't search for them 
- Auto retries a different uploader if slow or stuck
- Optional direct message captcha solver
- Sync playlists locally from Spotify, YouTube/Music, Apple Music, Amazon Music, Tidal, Deezer, Qobuz, Pandora and Beatport
- DJ mode for ensuring songs can play on any CDJ/XDJs

### Limitations
- Soulseek limits searches to 34 every 220 seconds. Some stuff may take a while.
- If YouTube or SoundCloud scanning or downloading breaks, do `docker compose build --pull --no-cache app` to force `yt-dlp` to update.
- YouTube audio is lossy (usually Opus around 130 kbps, sometimes AAC in `.m4a`) and is never converted, whatever formats are ticked, unless DJ mode is on. Some DJ software can't play Opus.
- SoundCloud audio is AAC in `.m4a`, usually 160 kbps, sometimes 96. Tracks under DRM (most major-label uploads) and Go+ previews are skipped, and so are edits, flips and mashups unless you search for one.
- Links are read the way each service's own web player reads them, without an account. If a service changes how its player works, its links can stop working until Song-DL is updated. Pandora links only work on a server in the US. Beatport's own playlists, and Amazon Music links to `/tracks/` (use the song's Share link instead), can't be read.

## Page List

| Page | Who can access | Description |
| --- | --- | --- |
| `/` | everyone | Search songs or playlists, download them (a playlist as a zip or separate files). Admins can also pick Separate files to Server in Download as: each song is saved on the server in a folder named after the playlist, and Import takes whole playlists (up to 10,000 songs) in the background |
| `/login/` | admins | Sign in (Cloudflare Turnstile optional) |
| `/downloads/` | admins | What's being saved on the server and playlist imports: pause, resume, cancel, delete |
| `/library/` | admins | Placeholder for now |

WebUI at `./webui/`
Server at `./server/songdl/`

## Self Hosting

You need `git` and `docker` first.

1. `git clone https://github.com/SlopHobbyist/song-dl`
2. Copy `config.yaml.example` to `config.yaml` and edit it
3. `docker compose up -d --build` to run
4. `docker compose down` to stop

Some Linux systems require `sudo docker` instead of just `docker`.

### Config
Copy `config.yaml.example` to `config.yaml` and edit before starting for the first time.

Type your Soulseek credentials, and admin credentials for the webui.

Optional turnstile `site_key` and `secret_key` go here too.

Behind Cloudflare or reverse proxies, set `webserver.trust_proxy: true`.

Files go to `./downloads` by default. Files by users not logged in deleted after `max_file_age` days. Admin downloaded stuff stays forever.

### Captcha Solver
Some Soulseek users use an extension to force downloaders to solve a text captcha in their direct messages. We can get around this.

Optional captcha solver enabled with `captcha.enabled: True` in `config.yaml`.

Run `sudo docker compose --profile captcha up -d` to download the 1GB LLM.

## Development

You need `git` `docker` `python` and `pip` first.

```
python -m venv .venv
.venv/bin/pip install -r server/requirements.txt pytest pytest-aiohttp   # .venv\Scripts\ on Windows
.venv/bin/python -m pytest
```

Tests run whole server against `server/tests/fake_slskd.py`. Just site can be tested without Soulseek too:

```
cd server
python -m tests.fake_slskd --downloads ../downloads
# then in another terminal, with SONGDL_SLSKD_URL=http://127.0.0.1:5030
python -m songdl setup && python -m songdl serve
```

## License
This repository and all contained code is licensed under `GNU GPLv3`. See `./LICENSE.txt` for more info.

[Slskd](https://github.com/slskd/slskd) is licensed under `GNU Affero GPLv3`.
Roboto, Roboto Mono, and Sora fonts are licensed under `SIL Open Font License 1.1`.
Material Icons font is licensed under `Apache License 2.0`.


## Disclosure

> **Note**: This entire project was written by AI (Claude Code: Opus 5.5). All code, architecture decisions, and implementation details were generated through AI assistance.
>
> I am a strong advocate for never mixing generated code into real repos. Projects like these should clearly disclose as such. I even have separate Github accounts for AI projects vs handwritten ones.