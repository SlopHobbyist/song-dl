> Untested don't use yet

# Song-DL

A simple web interface for downloading music off of Soulseek.

## About

This is a website for downloading music. It grabs songs from the Soulseek network using Slskd. Type a song name, and it'll find the highest quality version available. If Soulseek doesn't have it, it can fall back to the best audio on YouTube. Using CSV files, or by pasting a Spotify or YouTube playlist link, you can download playlists too. If you have a NAS, Admins can choose to download to the server instead.

## Differentiators

- Easy for noobs, simply search then download
- We filter out crap like remix, live versions, and files labeled "PRIVATE"
- Auto retries a different uploader if slow or stuck
- Optional captcha solver for direct messages
- Can paste Spotify and YouTube links to grab song lists automatically
- Falls back to YouTube when Soulseek doesn't have a song (pick where to look under **Sources**)

### Limitations
- Soulseek limits searches to 34 every 220 seconds. Some stuff may take a while.
- If youtube scanning or downloading breaks, do `docker compose build --pull --no-cache app` to force `yt-dlp` to update.
- YouTube audio is lossy (usually Opus around 130 kbps, sometimes AAC in `.m4a`) and is never converted, whatever formats are ticked. Some DJ software can't play Opus.

## Page List

| Page | Who can access | Description |
| --- | --- | --- |
| `/` | everyone | Search songs or playlists, download them |
| `/login/` | admins | Sign in (Cloudflare Turnstile optional) |
| `/server/` | admins | Same as `/`, but saved onto server instead. Import whole playlists (up to 10,000 songs) in the background |
| `/all/` | admins | Queue list, delete pause resume cancel stuff |

WebUI at `./webui/`
Server at `./server/songdl/`

## Self Hosting

You need `git` and `docker` first.

1. `git clone https://github.com/SlopHobbyist/song-dl`
2. Copy `./config.yaml.example` to `./config.yaml` and edit
3. `sudo docker compose up -d`

Files go to `./downloads` by default. Files by users not logged in deleted after `max_file_age` days. Admin downloaded stuff stays forever.

Behind Cloudflare or another reverse proxy, set `webserver.trust_proxy: true` so rate limits and login lockouts
see each visitor's own IP.

## Config
Copy `./config.yaml.example` to `./config.yaml` and edit before starting for the first time.
Make soulseek credentials, and admin credentials for the webui. Or leave the example ones, and random ones get saved into `config.yaml` on first start.

Captcha solver is off by default. To turn it on, set `captcha.enabled: True` and start with `sudo docker compose --profile captcha up -d`. First start downloads a ~1 GB model into `./models`.

## Development

You need `git` `docker` `python` and `pip` first.

```
python -m venv .venv
.venv/bin/pip install -r server/requirements.txt pytest pytest-aiohttp   # .venv\Scripts\ on Windows
.venv/bin/python -m pytest
```

Tests run whole server against `server/tests/fake_slskd.py`. Just site can be tested without soulseek too:

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