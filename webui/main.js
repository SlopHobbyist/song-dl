/* Song-DL web UI.
   Talks to the server's JSON API (server/songdl/web.py). The kit
   (design_language.js) supplies the drawer, dialogs, toasts, segmented control
   and file drop. */
(function () {
  'use strict';

  /* Dark only. The kit restores a stored theme choice on load, so pin dark
     again and forget any light choice left over from an earlier visit. */
  document.documentElement.setAttribute('data-theme', 'dark');
  try { localStorage.removeItem('design-language-theme'); } catch (e) {}

  var page = document.body.getAttribute('data-page');

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function toast(message, variant) {
    if (window.UI && UI.toast) UI.toast(message, { variant: variant });
  }

  /* ------------------------------------------------------- Accessibility
     Screen readers only announce changes inside a live region that already
     exists, so both regions are created up front. The kit's toast region is
     reused by UI.toast() when it is already in the page. */

  var toastRegion = document.createElement('div');
  toastRegion.className = 'toast-region';
  toastRegion.setAttribute('role', 'status');
  toastRegion.setAttribute('aria-live', 'polite');
  document.body.appendChild(toastRegion);

  var srStatus = document.createElement('div');
  srStatus.className = 'visually-hidden';
  srStatus.id = 'sr-status';
  srStatus.setAttribute('role', 'status');
  srStatus.setAttribute('aria-live', 'polite');
  srStatus.setAttribute('aria-atomic', 'true');
  document.body.appendChild(srStatus);

  /* Politely announces a message that has no visible toast (results appearing,
     a scan finishing). Clearing first makes a repeat of the same text speak. */
  function announce(message) {
    srStatus.textContent = '';
    setTimeout(function () { srStatus.textContent = message; }, 50);
  }

  /* The icon font's glyph names are real text ("upload_file"), so a screen
     reader would read them aloud. Hide every icon, including ones the page or
     the kit add later. */
  function hideIcons(root) {
    var icons = root.nodeType === 1 && root.matches('.icon') ? [root] : [];
    icons = icons.concat(Array.prototype.slice.call(root.querySelectorAll ? root.querySelectorAll('.icon:not([aria-hidden])') : []));
    icons.forEach(function (icon) { icon.setAttribute('aria-hidden', 'true'); });
  }
  hideIcons(document.body);
  new MutationObserver(function (mutations) {
    mutations.forEach(function (m) {
      Array.prototype.forEach.call(m.addedNodes, function (node) {
        if (node.nodeType === 1) hideIcons(node);
      });
    });
  }).observe(document.body, { childList: true, subtree: true });

  /* ------------------------------------------------------------ Helpers */

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  /* Writes text only when it differs, so live regions stay quiet on no-ops. */
  function setText(el, text) {
    text = String(text);
    if (el && el.textContent !== text) el.textContent = text;
  }

  function formatSize(bytes) {
    if (!bytes) return '—';
    var mb = bytes / 1e6;
    return mb >= 1000 ? (mb / 1000).toFixed(2) + ' GB' : mb.toFixed(1) + ' MB';
  }

  function sumSizes(items) {
    return items.reduce(function (sum, item) { return sum + (item.size || 0); }, 0);
  }

  function formatDuration(seconds) {
    if (!seconds) return '—';
    var s = Math.round(seconds);
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  function formatSpeed(bytesPerSecond) {
    return ((bytesPerSecond || 0) / 1e6).toFixed(1) + ' MB/s';
  }

  /* The folder admins save songs into, as the server names it (see /api/session) */
  var serverRoot = 'downloads/';

  /* The folder under serverRoot, with a trailing slash. */
  function subfolder() {
    var field = $('#subfolder');
    var value = field ? field.value.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '') : '';
    return value ? value + '/' : '';
  }

  /* A button that is busy keeps keyboard focus (aria-disabled, not disabled)
     and shows what it is doing in place of its own label. */
  function isBusy(button) { return button.getAttribute('aria-disabled') === 'true'; }

  function setBusy(button, label) {
    if (!button.hasAttribute('data-idle')) button.setAttribute('data-idle', button.innerHTML);
    button.setAttribute('aria-disabled', 'true');
    button.innerHTML = '<span class="icon" aria-hidden="true">hourglass_top</span> ' + escapeHtml(label);
  }

  function setIdle(button) {
    if (button.hasAttribute('data-idle')) {
      button.innerHTML = button.getAttribute('data-idle');
      button.removeAttribute('data-idle');
    }
    button.removeAttribute('aria-disabled');
  }

  /* ---------------------------------------------------------------- API */

  async function api(method, path, body, raw) {
    var options = { method: method, credentials: 'same-origin', headers: {} };
    if (raw !== undefined) {
      options.body = raw;
    } else if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    var res;
    try {
      res = await fetch(path, options);
    } catch (e) {
      throw new Error('Can\'t reach the server. Check your connection and try again.');
    }
    var data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      /* The session ended (or never began): the admin pages need a sign-in. */
      if (res.status === 401 && (page === 'server' || page === 'all')) {
        window.location.href = '/login/';
      }
      var error = new Error(data.error || 'Something went wrong. Please try again.');
      error.status = res.status;
      throw error;
    }
    return data;
  }

  /* Runs `handler` now and then every `ms` while the tab is visible. Tells the
     person once if the server stops answering. */
  function poll(handler, ms) {
    var running = false;
    var failures = 0;
    var warned = false;
    async function tick() {
      if (running || document.hidden) return;
      running = true;
      try {
        await handler();
        failures = 0;
        warned = false;
      } catch (e) {
        failures++;
        if (failures >= 3 && !warned) {
          warned = true;
          toast('Lost contact with the server. Retrying…', 'warning');
        }
      } finally {
        running = false;
      }
    }
    tick();
    setInterval(tick, ms);
    document.addEventListener('visibilitychange', tick);
  }

  /* ------------------------------------------------- Navigation and session */

  function initChrome() {
    $$('[data-action="logout"]').forEach(function (link) {
      link.addEventListener('click', async function (event) {
        event.preventDefault();
        try { await api('POST', '/api/logout'); } catch (e) {}
        window.location.href = '/';
      });
    });

    var admin = $('#admin-link');
    var affixes = $$('[data-server-root]');
    var queuePage = $('#queue-rows');
    if (admin || affixes.length || queuePage) {
      api('GET', '/api/session').then(function (session) {
        if (session.serverFolder) {
          serverRoot = session.serverFolder + '/';
          affixes.forEach(function (el) { setText(el, serverRoot); });
          var folder = $('#subfolder');
          if (folder) folder.dispatchEvent(new Event('input'));
        }
        /* Signed in? The home page gets the admin pages' navigation in place of
           the sign-in button, with the All page's count of active downloads. */
        if (!admin || !session.user) return;
        function signedIn(yes) {
          admin.hidden = yes;
          $$('[data-signed-in]').forEach(function (el) { el.hidden = !yes; });
        }
        signedIn(true);
        var signedOut = false;
        poll(async function () {
          if (signedOut) return;
          try {
            setActiveCount((await api('GET', '/api/queue?summary=1')).active);
          } catch (e) {
            if (e.status !== 401) throw e;
            /* The session ended while the page was open */
            signedOut = true;
            signedIn(false);
          }
        }, 5000);
      }, function () {});
    }
  }

  function setActiveCount(count) {
    $$('.badge-count, .nav-item-trailing').forEach(function (el) { setText(el, count); });
  }

  /* ------------------------------------------------- Formats and sources
     Both are a ticked, ordered list in a dialog: the first that works wins.
     Formats: the Soulseek formats to look for, best first; each song is
     fetched once, in the first one found. Sources: where to look, and in what
     order. Remembered in this browser, and used by searches, scans and imports
     alike. */

  var FORMAT_NAMES = { WAV: 'WAV', FLAC: 'FLAC', AIFF: 'AIFF', MP3: 'MP3' };
  var DEFAULT_FORMAT_ORDER = ['WAV', 'FLAC', 'AIFF', 'MP3'];
  var DEFAULT_FORMATS = ['WAV', 'FLAC'];
  var SOURCE_NAMES = { soulseek: 'Soulseek', soundcloud: 'SoundCloud', youtube: 'YouTube' };
  var DEFAULT_SOURCES = ['soulseek', 'soundcloud', 'youtube'];

  /* The places a result was looked for before the one it came from: ["Soulseek"] */
  function triedBefore(job, source) {
    var at = job.sources.indexOf(SOURCE_NAMES[source]);
    return at > 0 ? job.sources.slice(0, at) : [];
  }

  /* The ticked boxes of a dialog's list, in the order they're tried (top to bottom). */
  function chosenIn(name, defaults) {
    var boxes = $$('input[name="' + name + '"]');
    if (!boxes.length) return defaults.slice();
    return boxes.filter(function (box) { return box.checked && !box.disabled; })
      .map(function (box) { return box.value; });
  }

  function chosenFormats() { return chosenIn('format', DEFAULT_FORMATS); }
  function chosenSources() { return chosenIn('source', DEFAULT_SOURCES); }
  function usesSoulseek() { return chosenSources().indexOf('soulseek') > -1; }

  function loadSaved(key) {
    try {
      var saved = JSON.parse(localStorage.getItem(key) || 'null');
      return Array.isArray(saved) ? saved : null;
    } catch (e) { return null; }
  }

  /* A dialog's sortable list of checkboxes: puts back the saved order and ticks, saves them
     when they change, and calls apply() then. One box always stays ticked. */
  function initChoiceList(o) {
    var list = $(o.list);
    var boxes = $$('input[name="' + o.name + '"]');
    if (!list || !boxes.length) return;
    var chosen = function () { return chosenIn(o.name, o.defaults); };

    /* Every item's place, ticked or not. Before there was an order, only the ticked ones
       were saved, in the fixed order, which is as good a start as any */
    var ticked = loadSaved(o.key);
    var order = loadSaved(o.orderKey) || ticked || [];
    order = order.filter(function (name) { return o.names[name]; })
      .concat(o.order.filter(function (name) { return order.indexOf(name) < 0; }));
    order.forEach(function (name) {
      var item = $('[' + o.attr + '="' + name + '"]', list);
      if (item) list.appendChild(item);
    });
    if (ticked && ticked.length) {
      boxes.forEach(function (box) { if (!box.disabled) box.checked = ticked.indexOf(box.value) > -1; });
    }
    if (!chosen().length) {
      boxes.forEach(function (box) { if (!box.disabled) box.checked = o.defaults.indexOf(box.value) > -1; });
    }

    function save() {
      var all = $$('[' + o.attr + ']', list).map(function (item) { return item.getAttribute(o.attr); });
      try {
        localStorage.setItem(o.key, JSON.stringify(chosen()));
        localStorage.setItem(o.orderKey, JSON.stringify(all));
      } catch (e) {}
    }

    boxes.forEach(function (box) {
      box.addEventListener('change', function () {
        if (!chosen().length) {
          box.checked = true;
          toast(o.empty, 'warning');
        }
        save();
        o.apply();
      });
    });
    /* The kit's sortable list moves the rows itself (dragged, or by keyboard from the handle)
       and doesn't say so, so watch for the rows changing places */
    new MutationObserver(function () { save(); o.apply(); }).observe(list, { childList: true });
    o.apply();
  }

  function labelButtons(selector, label, title) {
    $$(selector).forEach(function (button) {
      button.title = title;
      button.setAttribute('aria-label', label + ': ' + title);
    });
  }

  /* The formats only choose what to get from Soulseek */
  function applyFormats() {
    var soulseek = usesSoulseek();
    labelButtons('[data-formats-button]', 'Formats', soulseek
      ? 'Downloads ' + chosenFormats().join(', else ')
      : 'Formats are for Soulseek, which is turned off in Sources');
    var fieldset = $('[data-formats-fieldset]');
    if (fieldset) fieldset.disabled = !soulseek;
    setText($('[data-formats-note]'), soulseek
      ? 'Each song is downloaded once, in the first of these Soulseek has.'
      : 'Formats are for Soulseek, which is turned off in Sources.');
  }

  function initFormats() {
    initChoiceList({
      list: '[data-formats-list]', name: 'format', attr: 'data-format',
      names: FORMAT_NAMES, order: DEFAULT_FORMAT_ORDER, defaults: DEFAULT_FORMATS,
      key: 'songdl-formats', orderKey: 'songdl-format-order',
      empty: 'Pick at least one format', apply: applyFormats
    });
  }

  function initSources() {
    initChoiceList({
      list: '[data-sources-list]', name: 'source', attr: 'data-source',
      names: SOURCE_NAMES, order: DEFAULT_SOURCES, defaults: DEFAULT_SOURCES,
      key: 'songdl-sources', orderKey: 'songdl-source-order',
      empty: 'Pick at least one place to look',
      apply: function () {
        var names = chosenSources().map(function (name) { return SOURCE_NAMES[name]; });
        labelButtons('[data-sources-button]', 'Sources', 'Looks on ' + names.join(', then '));
        applyFormats();
      }
    });
  }

  /* ------------------------------------------- Search / playlist switch */

  /* A search or scan that is polling stops as soon as its run number changes. */
  var runs = { search: 0, scan: 0, scanJob: null };

  function cancelScanJob() {
    runs.scan++;
    if (runs.scanJob) {
      fetch('/api/scan/' + runs.scanJob, { method: 'DELETE', credentials: 'same-origin' }).catch(function () {});
      runs.scanJob = null;
    }
  }

  function hideResults() {
    runs.search++;
    cancelScanJob();
    ['#search-result', '#search-loading', '#search-empty', '#scan-loading', '#scan-results'].forEach(function (sel) {
      var el = $(sel);
      if (el) el.hidden = true;
    });
    ['#scan-button', '#search-form button[type="submit"]'].forEach(function (sel) {
      var button = $(sel);
      if (button) setIdle(button);
    });
  }

  /* Shows the search or playlist panel. `focus` is what to focus after, or
     null for the panel's usual first control. */
  function showPanel(name, focus) {
    var title = $('#home-title');
    $$('[data-panel]').forEach(function (panel) {
      panel.hidden = panel.getAttribute('data-panel') !== name;
    });
    if (title) {
      title.textContent = title.getAttribute('data-title-' + name);
      announce(title.textContent);
    }
    hideResults();
    focus = focus || (name === 'playlist' ? $('#import-button') || $('#scan-button') : $('#q'));
    if (focus) focus.focus();
  }

  function initPanels() {
    $$('[data-show-panel]').forEach(function (button) {
      button.addEventListener('click', function () {
        showPanel(button.getAttribute('data-show-panel'));
      });
    });
  }

  /* --------------------------------------------------- Playlist links
     A link to a single YouTube video becomes a search for its song.
     A Spotify or YouTube playlist link, pasted in the song search box, is read
     by the server into a CSV of the whole playlist. The CSV then stands in for
     an uploaded file: it can be saved for later, or scanned like any other.
     The playlist panel only opens once there is a playlist: a link like that,
     or a CSV chosen with the Playlist button (or dropped on the search). */

  var SPOTIFY_PLAYLIST = /(?:\bopen\.spotify\.com\/(?:intl-[\w-]+\/)?(?:embed\/)?(?:user\/[^\/\s]+\/)?playlist\/|\bspotify:(?:user:[^:\s]+:)?playlist:)[A-Za-z0-9]{22}\b/;
  var SPOTIFY_OTHER = /\bopen\.spotify\.com\/(?:intl-[\w-]+\/)?(?:track|album|artist|episode|show)\//;
  /* youtube.com/playlist?list=, watch?v=…&list=, youtu.be/…?list=, music.youtube.com/… */
  var YOUTUBE_PLAYLIST = /(?:^|[\/.\s])(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)\/\S*?[?&]list=[A-Za-z0-9_-]{10,64}/;
  /* A single video: watch?v=, youtu.be/, shorts/, live/, embed/ (a link that names a playlist is the playlist's) */
  var YOUTUBE_VIDEO = /(?:^|[\/.\s])(?:(?:www|m|music)\.)?(?:youtube\.com\/(?:watch\?(?:\S*?&)?v=|shorts\/|live\/|embed\/)|youtu\.be\/)[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])/;
  var YOUTUBE_OTHER = /(?:^|[\/.\s])(?:(?:www|m|music)\.)?youtube\.com\/(?:@|channel\/|c\/|user\/)/;

  function isPlaylistLink(text) { return SPOTIFY_PLAYLIST.test(text || '') || YOUTUBE_PLAYLIST.test(text || ''); }
  function linkSource(text) { return YOUTUBE_PLAYLIST.test(text || '') ? 'YouTube' : 'Spotify'; }

  /* The playlist read from a link: { url, file, name, total, maxScan }, or null
     when the playlist panel holds an uploaded file (or nothing). */
  var linked = null;
  var linkRun = 0;
  /* The YouTube video a search came from: { query, id }. If Soulseek doesn't have
     the song, YouTube's fallback takes this video instead of searching. */
  var videoHint = null;

  /* Saves a File the page made (the playlist CSV) under its own name. */
  function saveBlob(file) {
    var url = URL.createObjectURL(file);
    var link = document.createElement('a');
    link.href = url;
    link.download = file.name;
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
  }

  /* The playlist the panel holds: the CSV read from a link, or the file chosen. */
  function chosenPlaylist() {
    var input = $('#csv-input');
    return linked ? linked.file : input && input.files && input.files[0];
  }

  function initPlaylistLink() {
    var list = $('#csv-list');
    var input = $('#csv-input');
    var pick = $('#playlist-button');
    var scanButton = $('#scan-button');
    var importButton = $('#import-button');
    var q = $('#q');
    if (!list || !scanButton || !input) return;

    function forgetLink() {
      linkRun++;
      linked = null;
      setIdle(scanButton);
      if (importButton) importButton.removeAttribute('aria-disabled');
    }

    function showFile(file) {
      list.innerHTML =
        '<div class="file-item">' +
          '<span class="icon icon-sm">description</span>' +
          '<span class="grow text-truncate" title="' + escapeHtml(file.name) + '">' + escapeHtml(file.name) + '</span>' +
          '<span class="text-caption text-muted">' + count(Math.max(1, Math.round(file.size / 1024))) + ' KB</span>' +
        '</div>';
    }

    /* A CSV was chosen (or dropped): it replaces any link, and opens the playlist panel */
    function useFile(file) {
      if (!/\.csv$/i.test(file.name) && file.type !== 'text/csv') {
        input.value = '';
        toast('That isn\'t a CSV file. Choose a playlist CSV, or paste a playlist link in the search box.', 'warning');
        return;
      }
      forgetLink();
      if (q) q.value = '';
      showFile(file);
      showPanel('playlist');
      announce(file.name + ' selected');
    }

    function showReading(source) {
      list.innerHTML =
        '<div class="file-item" aria-busy="true">' +
          '<span class="icon icon-sm">hourglass_top</span>' +
          '<span class="grow text-truncate">Reading the playlist from ' + source + '…' +
            (source === 'YouTube' ? ' A big one takes up to a minute.' : '') + '</span>' +
        '</div>';
    }

    function showLinked() {
      var limit = page === 'server' ? linked.maxImport : linked.maxScan;
      var tooBig = linked.total > limit;
      list.innerHTML =
        '<div class="file-item">' +
          '<span class="icon icon-sm">queue_music</span>' +
          '<span class="grow text-truncate" title="' + escapeHtml(linked.name) + '">' + escapeHtml(linked.name) + '</span>' +
          '<span class="text-caption ' + (tooBig ? 'text-warning' : 'text-muted') + '"' +
            (tooBig ? ' title="' + (page === 'server' ? 'Imports' : 'Scans') + ' are limited to ' + limit + ' songs"' : '') + '>' +
            count(linked.total) + ' song' + (linked.total === 1 ? '' : 's') + '</span>' +
          (linked.unavailable
            ? '<span class="text-caption text-muted" title="Deleted or private videos. They stay in the CSV, but there is nothing to search for.">' +
              linked.unavailable + ' unavailable</span>'
            : '') +
          '<button class="button button-outlined button-sm" type="button" data-action="save-csv"' +
            ' aria-label="Download ' + escapeHtml(linked.file.name) + '" title="Save the playlist as a CSV, to scan later">' +
            '<span class="icon icon-sm">download</span> CSV</button>' +
        '</div>';
    }

    async function read(url) {
      var run = ++linkRun;
      linked = null;
      input.value = '';
      var source = linkSource(url);
      showReading(source);
      setBusy(scanButton, 'Reading playlist');
      if (importButton) importButton.setAttribute('aria-disabled', 'true');
      announce('Reading the playlist from ' + source);
      try {
        var res = await api('POST', '/api/playlist-link', { url: url });
        if (run !== linkRun) return;
        linked = {
          url: url,
          file: new File([res.csv], res.filename, { type: 'text/csv' }),
          name: res.name,
          total: res.total,
          unavailable: res.unavailable || 0,
          maxScan: res.maxScan,
          maxImport: res.maxImport
        };
        showLinked();
        setIdle(scanButton);
        if (importButton) importButton.removeAttribute('aria-disabled');
        announce(res.name + ': ' + res.total + ' songs. Ready to ' + (importButton ? 'import or scan' : 'scan') +
          ', or download the CSV.');
        if (!scanButton.closest('[data-panel]').hidden) (importButton || scanButton).focus();
      } catch (e) {
        if (run !== linkRun) return;
        list.innerHTML = '';
        setIdle(scanButton);
        if (importButton) importButton.removeAttribute('aria-disabled');
        toast(e.message, 'danger');
      }
    }

    /* A single video: ask the server which song it is, then search for that */
    async function searchVideo(url) {
      var form = $('#search-form');
      var submit = form && $('button[type="submit"]', form);
      if (!form || !q) return;
      if (form.hidden) showPanel('search', q);
      q.value = url;
      setBusy(submit, 'Reading video');
      announce('Reading the video from YouTube');
      try {
        var res = await api('POST', '/api/video-link', { url: url });
        setIdle(submit);
        if (q.value.trim() !== url) return;   // typed over while it was read
        q.value = res.query;
        videoHint = res.video ? { query: res.query, id: res.video } : null;
        form.requestSubmit();
      } catch (e) {
        setIdle(submit);
        toast(e.message, 'danger');
      }
    }

    function tryLink(text, fromSearch) {
      text = (text || '').trim();
      if (isPlaylistLink(text)) {
        if (fromSearch) {
          q.value = '';
          showPanel('playlist', scanButton);
        }
        if (!linked || linked.url !== text) read(text);
        return true;
      }
      if (YOUTUBE_VIDEO.test(text)) {
        searchVideo(text);
        return true;
      }
      if (SPOTIFY_OTHER.test(text) || YOUTUBE_OTHER.test(text)) {
        toast('Only playlist links and YouTube video links work, not Spotify songs, albums, artists or channels.', 'warning');
        return true;
      }
      return false;
    }

    /* A link pasted (or dropped) in the song search box switches to the
       playlist panel. Typing doesn't, since a link is never typed. */
    if (q) {
      q.addEventListener('paste', function () {
        setTimeout(function () { tryLink(q.value, true); }, 0);
      });
      q.addEventListener('drop', function () {
        setTimeout(function () { tryLink(q.value, true); }, 0);
      });
    }

    /* The Playlist button opens the file picker */
    if (pick) pick.addEventListener('click', function () { input.click(); });
    input.addEventListener('change', function () {
      if (input.files && input.files.length) useFile(input.files[0]);
    });

    /* A CSV dragged anywhere onto the page is used like a chosen one. While a
       file is over the window, an overlay says so. It ignores the pointer, so
       the drag events still land on the page underneath; dragenter and
       dragleave come in pairs per element, so a count says when the drag has
       left the window. */
    function hasFiles(event) {
      return event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types || [], 'Files') > -1;
    }
    var overlay = document.createElement('div');
    overlay.className = 'drop-overlay';
    overlay.hidden = true;
    overlay.setAttribute('aria-hidden', 'true');
    overlay.innerHTML = '<div class="drop-overlay-box"><span class="icon icon-lg">upload_file</span>' +
      '<span class="text-heading-3">Drop a playlist CSV</span></div>';
    document.body.appendChild(overlay);
    var dragDepth = 0;
    function endDrag() { dragDepth = 0; overlay.hidden = true; }

    window.addEventListener('dragenter', function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      dragDepth++;
      overlay.hidden = false;
    });
    window.addEventListener('dragover', function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    });
    window.addEventListener('dragleave', function (event) {
      if (!hasFiles(event)) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) overlay.hidden = true;
    });
    window.addEventListener('dragend', endDrag);
    window.addEventListener('drop', function (event) {
      endDrag();
      if (!hasFiles(event)) return;
      event.preventDefault();   // else the browser opens the file in place of the page
      var files = event.dataTransfer.files;
      if (!files.length) return;
      try { input.files = files; } catch (e) { /* the File is still used below */ }
      useFile(files[0]);
    });

    /* Going back to the song search drops a playlist still being read. A read
       one is kept, so pasting the same link again doesn't read it twice; the
       file input is cleared so picking the same file again still works. */
    $$('[data-show-panel="search"]').forEach(function (button) {
      button.addEventListener('click', function () {
        if (!linked) forgetLink();
        input.value = '';
      });
    });

    list.addEventListener('click', function (event) {
      if (event.target.closest('[data-action="save-csv"]') && linked) {
        saveBlob(linked.file);
        announce('Downloading ' + linked.file.name);
      }
    });

    /* The search form hands a link over too, for a link pasted some other way */
    return function (query) { return tryLink(query, true); };
  }

  /* ------------------------------------------------------ Getting files */

  /* Asks the browser to save a file the server has ready. */
  function saveFile(url) {
    var link = document.createElement('a');
    link.href = url;
    link.setAttribute('download', '');
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  /* Has the server fetch each result from Soulseek, `concurrency` at a time,
     and waits for all of them. Returns the final state of every job, in order.
     onUpdate(summary) is called as it goes; see fetchSummary. The server stops
     a fetch once the page stops asking about it, so leaving the page cancels. */
  async function fetchFiles(resultIds, concurrency, onUpdate) {
    var jobs = new Array(resultIds.length);
    var views = resultIds.map(function () { return null; });
    var next = 0;

    function report() {
      if (onUpdate) onUpdate(fetchSummary(views));
    }

    async function worker() {
      while (next < resultIds.length) {
        var i = next++;
        try {
          var started = await api('POST', '/api/download', { result: resultIds[i] });
          var view;
          for (;;) {
            var res = await api('GET', '/api/downloads?id=' + encodeURIComponent(started.id));
            view = res.jobs[started.id];
            views[i] = view;
            report();
            if (view.state !== 'running') break;
            await sleep(1000);
          }
          view.id = started.id;
          jobs[i] = view;
        } catch (e) {
          jobs[i] = { state: 'failed', error: e.message };
        }
        views[i] = jobs[i];
        report();
      }
    }

    var workers = [];
    for (var w = 0; w < Math.min(concurrency, resultIds.length); w++) workers.push(worker());
    await Promise.all(workers);
    return jobs;
  }

  /* Where a set of fetches stands: { done, total, progress, speed, note, short }.
     progress is 0..1, or null while no bytes have arrived yet (requesting the
     file, waiting in the uploader's queue). A failed fetch counts as finished. */
  function fetchSummary(views) {
    var total = views.length;
    var done = 0, sum = 0, speed = 0, running = [];
    views.forEach(function (v) {
      if (!v) return;
      if (v.state !== 'running') { done++; sum += 1; return; }
      sum += v.progress || 0;
      speed += v.speed || 0;
      running.push(v);
    });
    var progress = sum > 0 ? Math.min(1, sum / total) : null;
    var note;
    if (total > 2) {
      note = done + ' of ' + total + ' files ready';
    } else {
      /* One song, maybe in two formats: say what each copy is doing */
      note = running.map(function (v) {
        return (running.length > 1 ? v.format + ': ' : '') + (v.note || 'Starting');
      }).join(' · ');
    }
    if (speed) note += (note ? ' · ' : '') + formatSpeed(speed);
    /* A word for small spaces: the percentage, or what it's waiting on */
    var queued = running.some(function (v) { return /queued/i.test(v.note || ''); });
    var short = progress !== null ? Math.round(progress * 100) + '%' : queued ? 'Queued' : 'Requesting';
    return { done: done, total: total, progress: progress, speed: speed, note: note, short: short };
  }

  /* Fills in a [data-fetch-status] bar from a fetchSummary. */
  function showFetch(el, summary) {
    if (!el) return;
    el.hidden = false;
    var bar = $('.progress-linear', el);
    var note = $('[data-fetch-note]', el);
    var pct = summary.progress === null ? null : Math.round(summary.progress * 100);
    bar.classList.toggle('is-indeterminate', pct === null);
    if (pct === null) {
      bar.removeAttribute('aria-valuenow');
      bar.setAttribute('aria-valuetext', summary.note || 'Starting');
    } else {
      bar.setAttribute('aria-valuenow', pct);
      bar.setAttribute('aria-valuetext', pct + '%');
    }
    $('.progress-bar', bar).style.setProperty('--value', (pct || 0) + '%');
    setText($('.progress-value', el), pct === null ? '' : pct + '%');
    setText(note, summary.note || 'Starting');
    note.title = summary.note || '';
  }

  function hideFetch(el) {
    if (el) el.hidden = true;
  }

  /* Hands finished files to the browser: one by one, or as a single zip. */
  async function deliver(jobs, label, zipName) {
    var done = jobs.filter(function (job) { return job.state === 'done'; });
    var failed = jobs.filter(function (job) { return job.state !== 'done'; });
    if (failed.length) {
      var reason = failed[0].error || 'The download failed.';
      toast(failed.length === jobs.length ? reason : failed.length + ' of ' + jobs.length + ' failed. ' + reason, 'danger');
    }
    if (!done.length) return false;

    if (zipName) {
      try {
        var zip = await api('POST', '/api/zip', { jobs: done.map(function (job) { return job.id; }), name: zipName });
        saveFile(zip.url);
      } catch (e) {
        toast(e.message, 'danger');
        return false;
      }
    } else {
      for (var i = 0; i < done.length; i++) {
        if (i) await sleep(500);
        saveFile(done[i].file);
      }
    }
    toast('Downloading ' + label, 'success');
    announce('Downloading ' + label);
    return true;
  }

  /* Queues results on the server (Server page). */
  async function queueResults(ids, label) {
    var folderField = $('#subfolder');
    var res = await api('POST', '/api/queue', { folder: folderField ? folderField.value : '', results: ids });
    if (res.queued) {
      toast('Queued ' + label, 'success');
    } else {
      toast('Already in the queue', 'warning');
    }
    return res;
  }

  /* ------------------------------------------------------ Single search */

  function initSearch() {
    var form = $('#search-form');
    var result = $('#search-result');
    if (!form || !result) return;
    var loading = $('#search-loading');
    var status = $('#search-status');
    var empty = $('#search-empty');
    var submit = $('button[type="submit"]', form);
    var current = null;   // what the result card shows: { results: [...] }

    function bind(key, value) {
      var el = $('[data-bind="' + key + '"]', result);
      if (el) el.textContent = value;
    }

    function showResult(job) {
      var found = job.results;
      current = job;
      bind('title', job.title || job.query);
      bind('artist', job.artist || 'Unknown artist');
      bind('album', job.album || '—');
      bind('duration', formatDuration(found[0].duration));
      bind('format', found.map(function (r) { return r.format; }).join(' + '));
      bind('quality', found.length === 1 ? found[0].quality : found.map(function (r) { return r.format + ' ' + r.quality; }).join(' · '));
      bind('size', formatSize(sumSizes(found)));
      bind('filename', found.map(function (r) { return r.filename; }).join(', '));
      bind('dest', serverRoot + subfolder());
      var source = found[0].source;
      var where = SOURCE_NAMES[source] || 'Soulseek';
      var elsewhere = source !== 'soulseek';
      var tried = triedBefore(job, source);
      var from = $('[data-bind-source]', result);
      if (elsewhere) {
        from.innerHTML = where + ' · <a href="' + escapeHtml(found[0].link) + '" target="_blank" rel="noopener noreferrer"' +
          ' title="Open it on ' + where + '">' + escapeHtml(found[0].from || where) + '</a>';
      } else {
        from.textContent = 'Soulseek';
      }

      /* Formats that weren't found, and copies that may be another version */
      var tags = $('.row.row-tight', result);
      $$('.result-extra', tags).forEach(function (tag) { tag.remove(); });
      var notes = (tried.length ? ['Not on ' + tried.join(' or ')] : [])
        .concat(elsewhere ? [] : job.missing.map(function (f) { return 'No ' + f; }));
      found.forEach(function (r) {
        if (r.warning && notes.indexOf(r.warning) < 0) notes.push(r.warning);
      });
      notes.forEach(function (text) {
        var tag = document.createElement('span');
        tag.className = 'tag tag-warning result-extra';
        tag.textContent = text;
        tags.appendChild(tag);
      });
      hideFetch($('[data-fetch-status]', result));
      result.hidden = false;
      var message = 'Found ' + (job.title || job.query) + (job.artist ? ' by ' + job.artist : '') + ', ' +
        found.map(function (r) { return r.format; }).join(' and ') + (elsewhere ? ' from ' + where + '.' : '.') +
        (tried.length ? ' Not on ' + tried.join(' or ') + '.' : '') +
        (!elsewhere && job.missing.length ? ' Not found: ' + job.missing.join(', ') + '.' : '');
      announce(message);
    }

    function showEmpty(job) {
      var what = job.missing.join(' or ');
      var soulseek = job.sources.indexOf('Soulseek') > -1;
      var others = job.sources.filter(function (s) { return s !== 'Soulseek'; });
      var message;
      if (soulseek && others.length) {
        message = 'Nobody on Soulseek is sharing ' + what + ' files matching that, and ' + others.join(' and ') +
          ' had nothing either. Try just the song title, or check the spelling.';
      } else if (soulseek) {
        message = 'Nobody on Soulseek is sharing ' + what + ' files matching that. Soulseek blocks searches for some ' +
          'artists, so try just the song title, or turn on SoundCloud or YouTube in Sources.';
      } else {
        message = others.join(' and ') + ' had nothing matching that. Try just the song title, or check the spelling.';
      }
      setText($('[data-bind="message"]', empty), message);
      empty.hidden = false;
      announce('No match found for ' + job.query);
    }

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (isBusy(submit)) return;
      var query = $('#q').value.trim();
      if (handOffLink && handOffLink(query)) return;
      var formats = chosenFormats();
      var sources = chosenSources();
      var soulseek = sources.indexOf('soulseek') > -1;
      var video = videoHint && videoHint.query === query ? videoHint.id : null;
      videoHint = null;
      if (soulseek && !formats.length) {
        toast('Pick at least one format', 'warning');
        return;
      }
      if (!query) {
        toast('Type a song name first', 'warning');
        $('#q').focus();
        return;
      }

      hideResults();
      var run = runs.search;
      setBusy(submit, 'Searching');
      var searching = 'Searching ' + SOURCE_NAMES[sources[0]];
      status.textContent = searching;
      loading.hidden = false;
      announce('Searching for ' + query);
      try {
        var body = { query: query, formats: formats, sources: sources };
        if (video) body.video = video;
        var started = await api('POST', '/api/search', body);
        var job;
        for (;;) {
          await sleep(1000);
          job = await api('GET', '/api/search/' + started.id);
          if (run !== runs.search) return;
          status.textContent = job.status || searching;
          if (job.state !== 'running') break;
        }
        loading.hidden = true;
        if (job.state === 'failed') {
          toast(job.error || 'The search failed. Please try again.', 'danger');
        } else if (!job.results.length) {
          showEmpty(job);
        } else {
          showResult(job);
        }
      } catch (e) {
        if (run === runs.search) toast(e.message, 'danger');
      } finally {
        if (run === runs.search) {
          loading.hidden = true;
          setIdle(submit);
        }
      }
    });

    var folder = $('#subfolder');
    if (folder) {
      folder.addEventListener('input', function () { bind('dest', serverRoot + subfolder()); });
    }

    result.addEventListener('click', async function (event) {
      var button = event.target.closest('[data-action]');
      if (!button || isBusy(button) || !current) return;
      var found = current.results;
      var title = current.title || current.query;
      var action = button.getAttribute('data-action');
      var ids = found.map(function (r) { return r.id; });

      if (action === 'download-one') {
        var meter = $('[data-fetch-status]', result);
        setBusy(button, 'Fetching');
        showFetch(meter, fetchSummary(ids.map(function () { return null; })));
        try {
          var jobs = await fetchFiles(ids, ids.length, function (summary) {
            setBusy(button, summary.progress !== null ? 'Fetching ' + summary.short : 'Fetching');
            showFetch(meter, summary);
          });
          await deliver(jobs, '"' + title + '"');
        } finally {
          setIdle(button);
          hideFetch(meter);
        }
      } else if (action === 'queue-one') {
        setBusy(button, 'Queueing');
        try {
          await queueResults(ids, '"' + title + '"');
        } catch (e) {
          toast(e.message, 'danger');
        } finally {
          setIdle(button);
        }
      }
    });
  }

  /* ------------------------------------------------------ Playlist scan */

  var SCAN_TAGS = {
    pending: '<span class="tag">Waiting</span>',
    searching: '<span class="tag tag-info">Searching</span>',
    found: '<span class="tag tag-success" title="Found on Soulseek">Found</span>',
    soundcloud: '<span class="tag tag-success" title="The best stream from SoundCloud">SoundCloud</span>',
    youtube: '<span class="tag tag-success" title="The best audio from YouTube">YouTube</span>',
    missing: '<span class="tag tag-danger">Not found</span>'
  };

  /* "soundcloud" or "youtube" for a song found there instead of on Soulseek, else null */
  function foundElsewhere(song) {
    var source = song.results.length > 0 ? song.results[0].source : 'soulseek';
    return source && source !== 'soulseek' ? source : null;
  }

  function scanTag(song) {
    return SCAN_TAGS[(song.state === 'found' && foundElsewhere(song)) || song.state];
  }

  function initScan() {
    var button = $('#scan-button');
    if (!button) return;
    var input = $('#csv-input');
    var loading = $('#scan-loading');
    var results = $('#scan-results');
    var rows = $('#scan-rows');
    var allButton = $('[data-action="download-all"], [data-action="queue-all"]', results);
    var server = page === 'server';
    var scan = null;   // { id, job, formats }

    function buildRows(job) {
      rows.innerHTML = job.tracks.map(function (song, i) {
        var action = server ? 'queue-row' : 'download-row';
        var icon = server ? 'playlist_add' : 'download';
        var label = (server ? 'Queue ' : 'Download ') + song.title;
        return '<tr data-index="' + i + '" data-state="">' +
          '<td class="table-numeric col-index">' + (i + 1) + '</td>' +
          '<td><div class="song-cell"><span class="text-medium">' + escapeHtml(song.title) + '</span>' +
            '<span class="text-caption text-muted">' + escapeHtml(song.artist || 'Unknown artist') + '</span></div></td>' +
          '<td class="cell-status"></td>' +
          '<td class="table-numeric hide-sm text-muted cell-size"></td>' +
          '<td class="col-action"><button class="button button-icon button-icon-sm" type="button" data-action="' + action + '"' +
            ' aria-label="' + escapeHtml(label) + '" title="' + escapeHtml(label) + '" disabled>' +
            '<span class="icon icon-sm">' + icon + '</span></button></td>' +
          '</tr>';
      }).join('');
    }

    function updateRows(job) {
      job.tracks.forEach(function (song, i) {
        var row = rows.children[i];
        var signature = song.state + ':' + song.results.length + ':' + foundElsewhere(song);
        if (!row || row.getAttribute('data-state') === signature) return;
        row.setAttribute('data-state', signature);
        row.classList.toggle('is-missing', song.state === 'missing');
        $('.cell-status', row).innerHTML = scanTag(song);
        setText($('.cell-size', row), song.size ? formatSize(song.size) : '—');
        $('[data-action]', row).disabled = song.state !== 'found';
      });
    }

    function updateSummary(job) {
      var found = job.tracks.filter(function (t) { return t.state === 'found'; }).length;
      var missing = job.tracks.filter(function (t) { return t.state === 'missing'; }).length;
      var left = job.total - found - missing;
      var elsewhere = ['soundcloud', 'youtube'].map(function (source) {
        var count = job.tracks.filter(function (t) { return t.state === 'found' && foundElsewhere(t) === source; }).length;
        return count ? count + ' on ' + SOURCE_NAMES[source] : '';
      }).filter(Boolean);
      var parts = ['<strong class="text-success">' + found + ' found</strong>' +
        (elsewhere.length ? ' (' + elsewhere.join(', ') + ')' : ''),
        missing + ' not found'];
      if (job.state === 'running') parts.push(left + ' left to scan');
      parts.push(escapeHtml(lookedFor(job.formats, job.sources)));
      if (job.skipped) parts.push(job.skipped + ' row' + (job.skipped === 1 ? '' : 's') + ' without a song name skipped');
      if (server) parts.push('<span class="text-mono">' + escapeHtml(serverRoot + subfolder()) + '</span>');
      $('[data-scan-summary]', results).innerHTML = parts.join(' · ');
      setText($('[data-scan-name]', results), job.name);

      if (!isBusy(allButton) || !allButton.hasAttribute('data-idle')) {
        var verb = server ? 'Queue all' : 'Download all';
        var icon = server ? 'playlist_add' : 'download';
        allButton.innerHTML = '<span class="icon" aria-hidden="true">' + icon + '</span> ' + verb + ' (' + found + ')';
        var ready = job.state !== 'running' && found > 0;
        if (ready) allButton.removeAttribute('aria-disabled'); else allButton.setAttribute('aria-disabled', 'true');
        allButton.title = job.state === 'running' ? 'Available when the scan finishes' : '';
      }
    }

    button.addEventListener('click', async function () {
      if (isBusy(button)) return;
      var file = chosenPlaylist();
      var formats = chosenFormats();
      var sources = chosenSources();
      if (!file) {
        toast('Choose a playlist CSV or paste a playlist link first', 'warning');
        return;
      }
      if (usesSoulseek() && !formats.length) {
        toast('Pick at least one format', 'warning');
        return;
      }
      if (file.size > 500 * 1024 || (linked && linked.total > linked.maxScan)) {
        var what = linked ? 'That playlist has ' + linked.total + ' songs.' : 'That file is too big.';
        toast(what + (page === 'server'
          ? ' A scan can have up to 300 songs. Use Import instead: it takes any size.'
          : ' A scan can have up to 300 songs. Download the CSV and split it into smaller files.'), 'warning');
        return;
      }

      cancelScanJob();
      var run = runs.scan;
      results.hidden = true;
      loading.hidden = false;
      setBusy(button, 'Uploading');
      announce('Scanning playlist');
      try {
        var started = await api('POST', '/api/scan?name=' + encodeURIComponent(file.name) +
          '&formats=' + encodeURIComponent(formats.join(',')) + '&sources=' + encodeURIComponent(sources.join(',')),
          undefined, await file.arrayBuffer());
        if (run !== runs.scan) return;
        runs.scanJob = started.id;
        scan = { id: started.id, job: null };
        var shown = false;
        for (;;) {
          var job = await api('GET', '/api/scan/' + started.id);
          if (run !== runs.scan) return;
          scan.job = job;
          if (job.state === 'failed') throw new Error(job.error || 'The scan failed. Please try again.');
          if (!shown) {
            shown = true;
            buildRows(job);
            loading.hidden = true;
            results.hidden = false;
            setIdle(button);
          }
          updateRows(job);
          updateSummary(job);
          if (job.state !== 'running') break;
          await sleep(1500);
        }
        runs.scanJob = null;
        var found = scan.job.tracks.filter(function (t) { return t.state === 'found'; }).length;
        announce('Scan complete. ' + found + ' songs found, ' + (scan.job.total - found) + ' not found.');
      } catch (e) {
        if (run === runs.scan) toast(e.message, 'danger');
      } finally {
        if (run === runs.scan) {
          loading.hidden = true;
          setIdle(button);
        }
      }
    });

    function resultIds(tracks) {
      var ids = [];
      tracks.forEach(function (t) { t.results.forEach(function (r) { ids.push(r.id); }); });
      return ids;
    }

    results.addEventListener('click', async function (event) {
      var target = event.target.closest('[data-action]');
      if (!target || target.disabled || isBusy(target) || !scan || !scan.job) return;
      var action = target.getAttribute('data-action');
      var found = scan.job.tracks.filter(function (t) { return t.state === 'found'; });

      try {
        if (action === 'download-all') {
          if (scan.job.state === 'running' || !found.length) return;
          setBusy(target, 'Fetching');
          var all = resultIds(found);
          var meter = $('[data-fetch-status]', results);
          showFetch(meter, fetchSummary(all.map(function () { return null; })));
          var jobs = await fetchFiles(all, 3, function (summary) {
            setBusy(target, summary.progress !== null ? 'Fetching ' + Math.round(summary.progress * 100) + '%' : 'Fetching');
            showFetch(meter, summary);
          });
          var zipName = scan.job.name.replace(/\.csv$/i, '');
          await deliver(jobs, found.length + ' songs', zipName);
        } else if (action === 'queue-all') {
          if (scan.job.state === 'running' || !found.length) return;
          setBusy(target, 'Queueing');
          var res = await queueResults(resultIds(found), found.length + ' songs');
          announce('Queued ' + res.queued + ' downloads');
        } else {
          var song = scan.job.tracks[+target.closest('tr').getAttribute('data-index')];
          var ids = song.results.map(function (r) { return r.id; });
          var label = '"' + song.title + '"';
          setBusy(target, '');
          target.innerHTML = '<span class="icon icon-sm" aria-hidden="true">hourglass_top</span>';
          var ok;
          if (action === 'download-row') {
            var statusCell = $('.cell-status', target.closest('tr'));
            try {
              ok = await deliver(await fetchFiles(ids, ids.length, function (summary) {
                target.innerHTML = summary.progress !== null
                  ? '<span class="fetch-pct" aria-hidden="true">' + summary.short + '</span>'
                  : '<span class="icon icon-sm" aria-hidden="true">hourglass_top</span>';
                target.title = summary.note;
                statusCell.innerHTML = '<span class="tag tag-info" title="' + escapeHtml(summary.note) + '">' +
                  escapeHtml(summary.progress !== null ? 'Downloading ' + summary.short : summary.short) + '</span>';
              }), label);
            } finally {
              statusCell.innerHTML = scanTag(song);
              target.title = target.getAttribute('aria-label');
            }
          } else {
            ok = (await queueResults(ids, label)).queued > 0;
          }
          /* Stay focusable (aria-disabled) so focus is not dropped. */
          var verb = action === 'download-row' ? 'Downloaded ' : 'Queued ';
          if (ok) {
            target.setAttribute('aria-label', verb + song.title);
            target.setAttribute('title', verb + song.title);
            target.innerHTML = '<span class="icon icon-sm text-success" aria-hidden="true">check</span>';
            target.removeAttribute('data-idle');
            return;
          }
          target.innerHTML = '<span class="icon icon-sm" aria-hidden="true">' + (action === 'download-row' ? 'download' : 'playlist_add') + '</span>';
          target.removeAttribute('data-idle');
          target.removeAttribute('aria-disabled');
        }
      } catch (e) {
        toast(e.message, 'danger');
        if (action === 'download-row' || action === 'queue-row') {
          target.innerHTML = '<span class="icon icon-sm" aria-hidden="true">' + (action === 'download-row' ? 'download' : 'playlist_add') + '</span>';
          target.removeAttribute('data-idle');
          target.removeAttribute('aria-disabled');
        }
      } finally {
        if (action === 'download-all' || action === 'queue-all') {
          hideFetch($('[data-fetch-status]', results));
          setIdle(target);
          updateSummary(scan.job);
        }
      }
    });
  }

  /* --------------------------------------------------- Playlist imports
     Server page: Import hands the whole playlist to the server, which searches
     it in the background and queues each song as it is found. The Server and
     All pages list the imports and their progress. */

  function formatEta(seconds) {
    if (seconds === null || seconds === undefined) return '';
    if (seconds < 60) return 'under a minute left';
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return 'about ' + minutes + ' min left';
    var hours = Math.floor(minutes / 60);
    minutes = minutes % 60;
    return 'about ' + hours + ' h' + (minutes ? ' ' + minutes + ' min' : '') + ' left';
  }

  function count(n) { return Number(n).toLocaleString(); }

  var IMPORT_TAG = {
    running:   '<span class="tag tag-info">Running</span>',
    paused:    '<span class="tag tag-warning">Paused</span>',
    cancelled: '<span class="tag tag-danger">Cancelled</span>',
    done:      '<span class="tag tag-success">Done</span>'
  };

  var refreshImports = null;   // set by initImports, so a new import shows at once

  /* What a scan or import looks for, in the order it's tried: "WAV, else FLAC", "WAV, else FLAC,
     else SoundCloud, else YouTube", "YouTube, else Soulseek WAV, else FLAC" or "YouTube". */
  function lookedFor(formats, sources) {
    sources = sources || ['Soulseek'];
    return sources.map(function (s, i) {
      if (s !== 'Soulseek') return s;
      return (i ? 'Soulseek ' : '') + formats.join(', else ');
    }).join(', else ');
  }

  function initImportButton() {
    var button = $('#import-button');
    if (!button) return;
    button.addEventListener('click', async function () {
      if (isBusy(button)) return;
      var file = chosenPlaylist();
      var formats = chosenFormats();
      if (!file) {
        toast('Choose a playlist CSV or paste a playlist link first', 'warning');
        return;
      }
      if (usesSoulseek() && !formats.length) {
        toast('Pick at least one format', 'warning');
        return;
      }
      if (file.size > 8 * 1024 * 1024) {
        toast('That file is too big. A playlist CSV should be under 8 MB.', 'warning');
        return;
      }
      setBusy(button, 'Importing');
      try {
        var imp = await api('POST', '/api/imports', {
          csv: await file.text(),
          name: linked ? linked.name : file.name,
          formats: formats,
          sources: chosenSources(),
          folder: $('#subfolder') ? $('#subfolder').value : ''
        });
        toast('Importing ' + count(imp.total) + ' songs into ' + serverRoot + imp.folder, 'success');
        if (refreshImports) await refreshImports();
        var section = $('#imports-section');
        if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } catch (e) {
        toast(e.message, 'danger');
      } finally {
        setIdle(button);
      }
    });
  }

  function importActions(imp) {
    var name = imp.name;   // actionButton escapes it
    var html = '';
    if (imp.state === 'running') html += actionButton('pause', 'pause', 'Pause', name);
    if (imp.state === 'paused') html += actionButton('resume', 'play_arrow', 'Resume', name);
    if (imp.state === 'cancelled') html += actionButton('resume', 'refresh', 'Restart', name);
    if (imp.state === 'running' || imp.state === 'paused') html += actionButton('cancel', 'block', 'Cancel', name);
    if (imp.missing || imp.elsewhere) {
      var label = 'Download the songs of ' + escapeHtml(name) + ' that weren\'t found, as a CSV';
      html += '<a class="button button-icon button-icon-sm" href="/api/imports/' + encodeURIComponent(imp.id) +
        '/missing.csv" download aria-label="' + label + '" title="' + label + '"><span class="icon icon-sm">playlist_remove</span></a>';
    }
    if (imp.state === 'done' || imp.state === 'cancelled') {
      html += actionButton('delete', 'delete', 'Remove', name);
    }
    return html;
  }

  function buildImport(imp) {
    var el = document.createElement('div');
    el.className = 'import-item';
    el.setAttribute('data-id', imp.id);
    el.innerHTML =
      '<div class="import-head">' +
        '<div class="import-title">' +
          '<span class="text-medium text-truncate" data-name></span>' +
          '<span class="text-caption text-muted text-truncate" data-where></span>' +
        '</div>' +
        '<span data-tag></span>' +
        '<div class="row-actions" data-actions></div>' +
      '</div>' +
      '<div class="progress-cell">' +
        '<div class="progress-linear" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar"></div></div>' +
        '<span class="text-caption text-muted progress-value" aria-hidden="true"></span>' +
      '</div>' +
      '<p class="text-caption text-muted" data-summary></p>' +
      '<p class="text-caption text-muted text-truncate" data-current hidden></p>' +
      '<p class="text-caption text-warning" data-note hidden></p>';
    $('.progress-linear', el).setAttribute('aria-label', 'Progress of ' + imp.name);
    return el;
  }

  function updateImport(el, imp) {
    setText($('[data-name]', el), imp.name);
    $('[data-name]', el).title = imp.name;
    setText($('[data-where]', el), lookedFor(imp.formats, imp.sources) + ' · ' + serverRoot + imp.folder + ' · by ' + imp.by);

    var signature = imp.state + '|' + imp.missing + '|' + imp.queued + '|' + imp.found;
    if (el.getAttribute('data-signature') !== signature) {
      el.setAttribute('data-signature', signature);
      $('[data-tag]', el).innerHTML = IMPORT_TAG[imp.state] || '';
      var cell = $('[data-actions]', el);
      var focused = cell.contains(document.activeElement) ? document.activeElement.getAttribute('data-action') : null;
      cell.innerHTML = importActions(imp);
      if (focused) {
        var next = $('[data-action="' + focused + '"]', cell) || $('[data-action]', cell);
        if (next) next.focus();
      }
    }

    var searched = imp.total - imp.left;
    var pct = imp.total ? Math.floor(searched / imp.total * 100) : 100;
    var bar = $('.progress-linear', el);
    bar.classList.toggle('progress-paused', imp.state === 'paused');
    bar.setAttribute('aria-valuenow', pct);
    bar.setAttribute('aria-valuetext', pct + ' percent searched');
    $('.progress-bar', bar).style.setProperty('--value', pct + '%');
    setText($('.progress-value', el), pct + '%');

    var parts = [count(searched) + ' of ' + count(imp.total) + ' searched',
      count(imp.found) + ' found', count(imp.missing) + ' not found'];
    if (imp.queued) parts.push(count(imp.queued) + ' queued');
    if (imp.skipped) parts.push(imp.skipped + ' row' + (imp.skipped === 1 ? '' : 's') + ' without a song name skipped');
    if (imp.state === 'running') parts.push(formatEta(imp.eta));
    setText($('[data-summary]', el), parts.filter(Boolean).join(' · '));

    var current = $('[data-current]', el);
    current.hidden = !(imp.state === 'running' && imp.current.length);
    setText(current, imp.current.length ? 'Searching: ' + imp.current.join(' · ') : '');
    var note = $('[data-note]', el);
    note.hidden = !imp.note;
    setText(note, imp.note || '');
  }

  function initImports() {
    var list = $('#import-list');
    var section = $('#imports-section');
    if (!list || !section) return;

    async function load() {
      var data = await api('GET', '/api/imports');
      var existing = {};
      $$('.import-item', list).forEach(function (el) { existing[el.getAttribute('data-id')] = el; });
      data.imports.forEach(function (imp, index) {
        var el = existing[imp.id] || buildImport(imp);
        delete existing[imp.id];
        updateImport(el, imp);
        if (list.children[index] !== el) list.insertBefore(el, list.children[index] || null);
      });
      Object.keys(existing).forEach(function (id) { existing[id].remove(); });
      section.hidden = !data.imports.length;
    }
    refreshImports = load;

    list.addEventListener('click', async function (event) {
      var button = event.target.closest('button[data-action]');
      if (!button) return;
      var el = button.closest('.import-item');
      var id = encodeURIComponent(el.getAttribute('data-id'));
      var name = $('[data-name]', el).textContent;
      var action = button.getAttribute('data-action');
      try {
        if (action === 'delete') {
          await api('DELETE', '/api/imports/' + id);
          toast('Removed "' + name + '". The songs it queued stay in the queue.');
        } else {
          await api('POST', '/api/imports/' + id + '/' + action);
          toast({ pause: 'Paused', resume: 'Resumed', cancel: 'Cancelled' }[action] + ' "' + name + '"');
        }
        await load();
      } catch (e) {
        toast(e.message, 'danger');
      }
    });

    poll(load, 3000);
  }

  /* -------------------------------------------------------- Queue rows
     One row renderer for both the server page's "Your queue" and the All
     page. The tbody's data-columns says which cells it has. Status is its
     own column; progress sits in the next one, the bar with its percentage
     beside it. Rows are kept between refreshes and only touched when
     something about them changed, so focus stays where it is. */

  var STATUS_TAG = {
    downloading: '<span class="tag tag-info">Downloading</span>',
    queued:      '<span class="tag">Queued</span>',
    paused:      '<span class="tag tag-warning">Paused</span>',
    cancelled:   '<span class="tag tag-danger">Cancelled</span>',
    failed:      '<span class="tag tag-danger">Failed</span>',
    done:        '<span class="tag tag-success">Done</span>'
  };

  function rowName(row) {
    var el = $('[data-name]', row);
    return el ? el.textContent : 'song';
  }

  function progressText(row, value) {
    return value + ' percent' + (row.getAttribute('data-status') === 'paused' ? ', paused' : '');
  }

  function progressHtml(row) {
    var status = row.getAttribute('data-status');
    var value = +row.getAttribute('data-progress') || 0;
    if (status === 'downloading' || status === 'paused') {
      var note = row.getAttribute('data-note');
      return '<div class="progress-cell"' + (note ? ' title="' + escapeHtml(note) + '"' : '') + '>' +
        '<div class="progress-linear' + (status === 'paused' ? ' progress-paused' : '') + '" role="progressbar"' +
          ' aria-valuenow="' + value + '" aria-valuemin="0" aria-valuemax="100"' +
          ' aria-valuetext="' + progressText(row, value) + '" aria-label="Progress of ' + escapeHtml(rowName(row)) + '">' +
          '<div class="progress-bar" style="--value: ' + value + '%;"></div></div>' +
        '<span class="text-caption text-muted progress-value" aria-hidden="true">' + value + '%</span>' +
      '</div>';
    }
    var text = {
      queued: 'Waiting',
      cancelled: '—',
      failed: row.getAttribute('data-note') || 'Error',
      done: row.getAttribute('data-size') || ''
    }[status];
    return '<span class="text-caption text-muted">' + escapeHtml(text) + '</span>';
  }

  function actionButton(action, icon, label, name) {
    label = label + ' ' + escapeHtml(name);
    return '<button class="button button-icon button-icon-sm" type="button" data-action="' + action +
      '" aria-label="' + label + '" title="' + label + '"><span class="icon icon-sm">' + icon + '</span></button>';
  }

  function actionsHtml(row) {
    var status = row.getAttribute('data-status');
    var name = rowName(row);
    var html = '';
    if (status === 'downloading' || status === 'queued') html += actionButton('pause', 'pause', 'Pause', name);
    if (status === 'paused' || status === 'cancelled') html += actionButton('resume', 'play_arrow', 'Resume', name);
    if (status === 'failed') html += actionButton('resume', 'refresh', 'Retry', name);
    if (status === 'downloading' || status === 'queued' || status === 'paused') html += actionButton('cancel', 'block', 'Cancel', name);
    return html + actionButton('delete', 'delete', 'Delete', name);
  }

  /* Redraws a cell, keeping keyboard focus on the same kind of button. */
  function renderRow(row) {
    var cell;
    if ((cell = $('.cell-status', row))) cell.innerHTML = STATUS_TAG[row.getAttribute('data-status')];
    if ((cell = $('.cell-progress', row))) cell.innerHTML = progressHtml(row);
    if ((cell = $('.row-actions', row))) {
      var focused = cell.contains(document.activeElement) ? document.activeElement.getAttribute('data-action') : null;
      cell.innerHTML = actionsHtml(row);
      if (focused) {
        var next = $('[data-action="' + focused + '"]', cell) || $('[data-action]', cell);
        if (next) next.focus();
      }
    }
  }

  function applyItem(row, item) {
    row.setAttribute('data-status', item.status);
    row.setAttribute('data-progress', item.progress || 0);
    row.setAttribute('data-size', item.size ? formatSize(item.size) : '');
    row.setAttribute('data-bytes', item.size || 0);
    row.setAttribute('data-note', item.note || '');
    row.setAttribute('data-finished', item.finished || 0);
  }

  function buildRow(item, columns) {
    var row = document.createElement('tr');
    row.setAttribute('data-id', item.id);
    applyItem(row, item);
    row.innerHTML = columns.map(function (col) {
      switch (col) {
        case 'song':
          return '<td class="cell-song"><div class="song-cell"><span class="text-medium" data-name>' + escapeHtml(item.title) + '</span>' +
            '<span class="text-caption text-muted">' + escapeHtml((item.artist ? item.artist + ' · ' : '') + item.format) + '</span></div></td>';
        case 'folder':   return '<td class="hide-sm text-mono">' + escapeHtml(serverRoot + item.folder) + '</td>';
        case 'by':       return '<td class="hide-lg">' + escapeHtml(item.by) + '</td>';
        case 'status':   return '<td class="cell-status"></td>';
        case 'progress': return '<td class="cell-progress"></td>';
        case 'actions':  return '<td class="row-actions"></td>';
      }
      return '';
    }).join('');
    renderRow(row);
    return row;
  }

  function updateRow(row, item) {
    var before = ['data-status', 'data-note', 'data-size'].map(function (a) { return row.getAttribute(a); }).join('|');
    var progress = row.getAttribute('data-progress');
    applyItem(row, item);
    var after = ['data-status', 'data-note', 'data-size'].map(function (a) { return row.getAttribute(a); }).join('|');
    if (before !== after) {
      renderRow(row);
    } else if (progress !== row.getAttribute('data-progress')) {
      /* Only the percentage moved: nudge the bar rather than rebuild it. */
      var bar = $('.progress-linear', row);
      if (!bar) return;
      var value = +row.getAttribute('data-progress');
      bar.setAttribute('aria-valuenow', value);
      bar.setAttribute('aria-valuetext', progressText(row, value));
      $('.progress-bar', bar).style.setProperty('--value', value + '%');
      setText($('.progress-value', row), value + '%');
    }
  }

  /* Makes the table body list exactly these items, in this order. */
  function syncRows(tbody, items, columns) {
    var existing = {};
    $$('tr', tbody).forEach(function (row) { existing[row.getAttribute('data-id')] = row; });
    var seen = {};
    items.forEach(function (item, index) {
      var row = existing[item.id];
      if (row) updateRow(row, item); else row = buildRow(item, columns);
      seen[item.id] = true;
      var at = tbody.children[index];
      if (at !== row) tbody.insertBefore(row, at || null);
    });
    Object.keys(existing).forEach(function (id) {
      if (!seen[id]) existing[id].remove();
    });
  }

  /* ------------------------------------------------ Server: your queue */

  function initMyQueue() {
    if (page !== 'server') return;
    var tbody = $('#queue-rows');
    var columns = tbody.getAttribute('data-columns').split(' ');
    poll(async function () {
      var data = await api('GET', '/api/queue?mine=1');
      setActiveCount(data.active);
      syncRows(tbody, data.items, columns);
      var more = $('#queue-more');
      if (more) {
        more.hidden = !data.more;
        setText(more, data.more ? 'And ' + count(data.more) + ' more. The All page lists everything.' : '');
      }
    }, 1500);
  }

  /* --------------------------------------------------------- All page */

  var STOPPED = ['paused', 'cancelled', 'failed'];
  var FILTERS = {
    all: function () { return true; },
    active: function (s) { return s === 'downloading' || s === 'queued'; },
    stopped: function (s) { return STOPPED.indexOf(s) > -1; },
    done: function (s) { return s === 'done'; }
  };

  function initAll() {
    if (page !== 'all') return;
    var tbody = $('#queue-rows');
    var columns = tbody.getAttribute('data-columns').split(' ');
    var filter = 'all';
    var search = $('#queue-search');
    var pendingDelete = null;
    var dialog = $('#dlg-delete');
    var speed = 0;

    function nameOf(row) { return $('[data-name]', row).textContent; }

    function refresh() {
      var term = (search.value || '').trim().toLowerCase();
      var rows = $$('tr', tbody);
      var visible = 0;
      var counts = { downloading: 0, queued: 0, stopped: 0, done: 0 };
      var midnight = new Date().setHours(0, 0, 0, 0);
      var doneBytes = 0;
      rows.forEach(function (row) {
        var status = row.getAttribute('data-status');
        if (STOPPED.indexOf(status) > -1) {
          counts.stopped++;
        } else if (status !== 'done') {
          counts[status]++;
        } else if (+row.getAttribute('data-finished') >= midnight) {
          /* "Done today" counts what finished since midnight */
          counts.done++;
          doneBytes += +row.getAttribute('data-bytes');
        }
        var show = FILTERS[filter](status) && (!term || row.textContent.toLowerCase().indexOf(term) > -1);
        row.hidden = !show;
        if (show) visible++;
      });
      Object.keys(counts).forEach(function (key) {
        setText($('[data-count="' + key + '"]'), counts[key]);
      });
      setText($('[data-bind="speed"]'), formatSpeed(speed));
      setText($('[data-bind="done-size"]'), doneBytes ? formatSize(doneBytes) : '0 MB');
      $('#queue-empty').hidden = visible > 0;
      setText($('#queue-summary'), visible + ' of ' + rows.length + ' items');
    }

    async function load() {
      var data = await api('GET', '/api/queue');
      speed = data.speed;
      setActiveCount(data.active);
      syncRows(tbody, data.items, columns);
      refresh();
    }

    $('#status-filter').addEventListener('click', function (event) {
      var button = event.target.closest('[data-filter]');
      if (!button) return;
      filter = button.getAttribute('data-filter');
      refresh();
    });
    search.addEventListener('input', refresh);

    tbody.addEventListener('click', async function (event) {
      var button = event.target.closest('[data-action]');
      if (!button) return;
      var row = button.closest('tr');
      var name = nameOf(row);
      var id = row.getAttribute('data-id');
      var action = button.getAttribute('data-action');
      if (action === 'delete') {
        pendingDelete = row;
        $('#dlg-delete-name').textContent = name;
        UI.openDialog('dlg-delete');
        return;
      }
      try {
        await api('POST', '/api/queue/' + encodeURIComponent(id) + '/' + action);
        toast({ pause: 'Paused', resume: 'Resumed', cancel: 'Cancelled' }[action] + ' "' + name + '"');
        await load();
      } catch (e) {
        toast(e.message, 'danger');
      }
    });

    dialog.addEventListener('close', async function () {
      var row = pendingDelete;
      pendingDelete = null;
      if (dialog.returnValue !== 'delete' || !row) return;
      var name = nameOf(row);
      var sibling = row.nextElementSibling;
      while (sibling && sibling.hidden) sibling = sibling.nextElementSibling;
      try {
        await api('DELETE', '/api/queue/' + encodeURIComponent(row.getAttribute('data-id')));
        toast('Deleted "' + name + '"', 'success');
        await load();
      } catch (e) {
        toast(e.message, 'danger');
      }
      var target = sibling && sibling.isConnected && $('.row-actions [data-action]', sibling);
      (target || search).focus();
    });

    $('[data-action="pause-all"]').addEventListener('click', async function () {
      try {
        var res = await api('POST', '/api/queue/pause-all');
        toast(res.paused ? 'Paused everything' : 'Nothing to pause');
        await load();
      } catch (e) {
        toast(e.message, 'danger');
      }
    });
    $('[data-action="clear-done"]').addEventListener('click', async function () {
      try {
        var res = await api('POST', '/api/queue/clear-done');
        toast('Cleared ' + res.cleared + ' finished');
        await load();
      } catch (e) {
        toast(e.message, 'danger');
      }
    });

    poll(load, 1500);
  }

  /* ------------------------------------------------------------ Login */

  function initLogin() {
    var form = $('#login-form');
    if (!form) return;
    var errorEl = $('#login-error');
    var placeholder = $('#login-turnstile');
    var submit = $('button[type="submit"]', form);
    var widget = false;

    function showError(message) {
      errorEl.innerHTML = '<span class="icon icon-sm">error</span> ' + escapeHtml(message);
      errorEl.hidden = false;
    }

    api('GET', '/api/session').then(function (session) {
      if (session.user) {
        window.location.href = '/server/';
        return;
      }
      if (!session.adminEnabled) {
        showError('Admin sign-in is not set up on this server.');
        submit.setAttribute('aria-disabled', 'true');
      }
      if (session.turnstileSiteKey) {
        /* Swap the placeholder for the real Cloudflare Turnstile widget. */
        widget = true;
        var el = document.createElement('div');
        el.className = 'cf-turnstile';
        el.setAttribute('data-sitekey', session.turnstileSiteKey);
        el.setAttribute('data-theme', 'dark');
        placeholder.replaceWith(el);
        var script = document.createElement('script');
        script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
        script.async = true;
        script.defer = true;
        document.head.appendChild(script);
      } else {
        /* No Turnstile configured: nothing to show */
        placeholder.hidden = true;
      }
    }, function (e) { showError(e.message); });

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (isBusy(submit)) return;
      errorEl.hidden = true;
      var token = form.querySelector('[name="cf-turnstile-response"]');
      if (widget && !(token && token.value)) {
        showError('Please complete the security check first.');
        return;
      }
      setBusy(submit, 'Signing in');
      try {
        await api('POST', '/api/login', {
          username: $('#login-user').value,
          password: $('#login-pass').value,
          remember: $('#login-remember').checked,
          turnstile: token ? token.value : null
        });
        window.location.href = '/server/';
      } catch (e) {
        showError(e.message);
        $('#login-pass').value = '';
        $('#login-pass').focus();
        if (widget && window.turnstile) window.turnstile.reset();
        setIdle(submit);
      }
    });
  }

  initChrome();
  initFormats();
  initSources();
  initPanels();
  var handOffLink = initPlaylistLink();
  initSearch();
  initScan();
  initImportButton();
  initImports();
  initMyQueue();
  initAll();
  initLogin();

  /* Remember the server folder between visits */
  var folderField = $('#subfolder');
  if (folderField) {
    try { folderField.value = localStorage.getItem('songdl-folder') || ''; } catch (e) {}
    folderField.dispatchEvent(new Event('input'));
    folderField.addEventListener('input', function () {
      try { localStorage.setItem('songdl-folder', folderField.value); } catch (e) {}
    });
  }
})();
