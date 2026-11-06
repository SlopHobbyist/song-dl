# Song-DL site

## About
This is a website that allows users to download songs.

## Regular
`/` (webui/index.html)
For regular non logged in users.
- Type in a song name
- Choose any of WAV, AIFF, FLAC, MP3 (checkboxes; WAV is on by default)
- Sources button: a dialog to tick where to look and drag them into the order they're tried (default: Soulseek, SoundCloud, YouTube). Saved in localStorage.
  Remembered in the browser; scans and imports use it too
- Press Search button
- Shows found file info
- Press Download button
- Downloads from browser to their computer

or

- Press the Playlist button to choose a playlist's song csv, or a .txt file with one song per line like `Artist - Song` (or drop either anywhere on the page),
  or paste a Spotify or YouTube playlist link in the search box
- The playlist panel opens only then, showing the playlist in a bar like the search box; its X removes it and goes back
- Formats (WAV, FLAC, AIFF, MP3) and Sources are ticked and dragged into order in their own dialogs;
  each song is downloaded once, in the first format Soulseek has (WAV, else FLAC by default)
- Press Upload and scan button
- Shows list of songs, if they were found on the server or not
- Press Individual Download button or Download All button
- Downloads from browser to their computer

## Server Mode
For admins logged in
Same flows as the regular mode, except:
- Choose a name for the subfolder where the files go
- Files are downloaded to the server, not to the user
- Can leave or refresh the page, or log in somewhere else, and queue is still there downloading

## All
For admins logged in.
Shows all things queued and downloading currently.
Can cancel resume and delete.

## Design
Uses the vendored `web-design-wavefront`.
Simple homepage. Navbar, centered search box, search button.