# Song-DL site

## About
This is a website that allows users to download songs.

## Regular
`/` (webui/index.html)
For regular non logged in users.
- Type in a song name
- Choose any of WAV, AIFF, FLAC, MP3 (checkboxes; WAV is on by default)
- Sources button: a dialog to tick where to look, tried in order: Soulseek, SoundCloud (coming soon), YouTube.
  Remembered in the browser; scans and imports use it too
- Press Search button
- Shows found file info
- Press Download button
- Downloads from browser to their computer

or

- Upload a playlist's song csv
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