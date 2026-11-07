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
- Not found button: saves the songs that weren't found as a .txt or .csv, to look for elsewhere
- Press Individual Download button or Download All button
- Download as dialog: Download all gives a Zip file or Separate files
- Downloads from browser to their computer

## Admins
Signed in admins use the same page. The menu shows Search, Downloads and Library.
- Download as has a third choice, Separate files to Server: Download all and the row buttons save the songs on
  the server, in `<server folder>/<playlist name>/`, not to the user
- Import (shown next to Upload and scan with that choice) saves a playlist of any size in the background
- Can leave or refresh the page, or log in somewhere else, and the queue is still there downloading

## Downloads
`/downloads/`, for admins logged in.
Shows all things queued and downloading currently, and the playlist imports.
Can cancel resume and delete.

## Library
`/library/`, for admins logged in.
- A list of the playlists saved on the server: every folder in `<server folder>/`, except the folder visitors'
  downloads go in (and Song-DL's own data folders)
- Each shows its song count and size; a filter box narrows the list by name
- Clicking one drops it open and lists its songs: title and artist, format, bitrate (bit depth, sample rate,
  length and size on hover), a download button and a delete button (asks first, then deletes the file from the server)

## Design
Uses the vendored `web-design-wavefront`.
Simple homepage. Navbar, centered search box, search button.