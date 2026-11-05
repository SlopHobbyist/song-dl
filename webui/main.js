/* Song-DL mockup.
   Nothing here talks to a server. Every button swaps in canned data so the
   flows can be clicked through; the kit (design_language.js) supplies the
   drawer, dialogs, toasts, segmented control and file drop. */
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

  /* The file drop is a div acting as a button: give it the keys a button has. */
  $$('.file-drop').forEach(function (zone) {
    zone.addEventListener('keydown', function (event) {
      if (event.target !== zone) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        zone.click();
      }
    });
  });
  $$('.file-drop input[type="file"]').forEach(function (input) {
    input.setAttribute('tabindex', '-1');
    input.setAttribute('aria-hidden', 'true');
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (file) announce(file.name + ' selected');
    });
  });

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* Every ticked format checkbox in the group, in page order. */
  function checkedValues(name) {
    return $$('input[name="' + name + '"]:checked').map(function (input) { return input.value; });
  }

  /* Writes text only when it differs, so live regions stay quiet on no-ops. */
  function setText(el, text) {
    text = String(text);
    if (el && el.textContent !== text) el.textContent = text;
  }

  /* "44.4 MB" + "48.6 MB" -> "93.0 MB" */
  function sumSizes(sizes) {
    var total = sizes.reduce(function (sum, s) { return sum + (parseFloat(s) || 0); }, 0);
    return total.toFixed(1) + ' MB';
  }

  /* The folder under downloads/, with a trailing slash. */
  function subfolder() {
    var field = $('#subfolder');
    var value = field ? field.value.trim().replace(/^\/+|\/+$/g, '') : '';
    return value ? value + '/' : '';
  }

  /* -------------------------------------------------------- Canned data */

  var FORMAT_INFO = {
    WAV:  { quality: '16-bit / 44.1 kHz', size: '44.4 MB', ext: 'wav' },
    AIFF: { quality: '16-bit / 44.1 kHz', size: '44.4 MB', ext: 'aiff' },
    FLAC: { quality: '24-bit / 96 kHz', size: '48.6 MB', ext: 'flac' },
    MP3:  { quality: '320 kbps', size: '9.8 MB', ext: 'mp3' }
  };

  var PLAYLIST = [
    { title: 'Paper Lanterns',    artist: 'The Quiet Harbor',  size: '48.6 MB', found: true },
    { title: 'Glasswing',         artist: 'Northbound Static', size: '50.1 MB', found: true },
    { title: 'Copper Season',     artist: 'Marlow Fields',     size: '41.2 MB', found: true },
    { title: 'Small Hours',       artist: 'Juniper Avenue',    size: '37.9 MB', found: true },
    { title: 'Neon Orchard',      artist: 'Vela Coast',        size: null,      found: false },
    { title: 'Rooftop Choir',     artist: 'Bellweather',       size: '39.2 MB', found: true },
    { title: 'Morning Ferry',     artist: 'Bellweather',       size: '54.1 MB', found: true },
    { title: 'Salt & Cedar',      artist: 'Ida Fontaine',      size: null,      found: false },
    { title: 'Weather Vane',      artist: 'Ottoline',          size: '46.0 MB', found: true },
    { title: 'Signal Fires',      artist: 'The Dunmore Set',   size: '43.3 MB', found: true },
    { title: 'Last Train West',   artist: 'Harrow & Pine',     size: null,      found: false },
    { title: 'Lighthouse Keeper', artist: 'Ottoline',          size: '52.7 MB', found: true }
  ];

  var QUEUE = [
    { title: 'Glasswing',         artist: 'Northbound Static', format: 'FLAC', folder: 'road-trip-2026/',     by: 'admin', status: 'downloading', progress: 62, size: '50.1 MB' },
    { title: 'Copper Season',     artist: 'Marlow Fields',     format: 'FLAC', folder: 'road-trip-2026/',     by: 'admin', status: 'downloading', progress: 18, size: '41.2 MB' },
    { title: 'Small Hours',       artist: 'Juniper Avenue',    format: 'FLAC', folder: 'road-trip-2026/',     by: 'admin', status: 'queued',      size: '37.9 MB' },
    { title: 'Weather Vane',      artist: 'Ottoline',          format: 'WAV',  folder: 'archive/vinyl-rips/', by: 'sam',   status: 'queued',      size: '46.0 MB' },
    { title: 'Lighthouse Keeper', artist: 'Ottoline',          format: 'WAV',  folder: 'archive/vinyl-rips/', by: 'sam',   status: 'paused',      progress: 41, size: '52.7 MB' },
    { title: 'Signal Fires',      artist: 'The Dunmore Set',   format: 'FLAC', folder: 'road-trip-2026/',     by: 'admin', status: 'failed',      note: 'Source timed out' },
    { title: 'Paper Lanterns',    artist: 'The Quiet Harbor',  format: 'FLAC', folder: 'road-trip-2026/',     by: 'admin', status: 'done',        size: '48.6 MB' },
    { title: 'Rooftop Choir',     artist: 'Bellweather',       format: 'FLAC', folder: 'sunday-mix/',         by: 'sam',   status: 'done',        size: '39.2 MB' },
    { title: 'Morning Ferry',     artist: 'Bellweather',       format: 'FLAC', folder: 'sunday-mix/',         by: 'sam',   status: 'done',        size: '54.1 MB' }
  ];

  /* "Title - Artist" or "Title by Artist"; anything else is just a title. */
  function parseQuery(query) {
    var parts = query.split(/\s+(?:-|–|by)\s+/i);
    if (parts.length >= 2) return { title: parts[0], artist: parts.slice(1).join(' ') };
    return { title: query || 'Paper Lanterns', artist: query ? 'Unknown artist' : 'The Quiet Harbor' };
  }

  /* ------------------------------------------- Search / playlist switch */

  function hideResults() {
    ['#search-result', '#scan-loading', '#scan-results'].forEach(function (sel) {
      var el = $(sel);
      if (el) el.hidden = true;
    });
  }

  function initPanels() {
    var title = $('#home-title');
    $$('[data-show-panel]').forEach(function (button) {
      button.addEventListener('click', function () {
        var name = button.getAttribute('data-show-panel');
        $$('[data-panel]').forEach(function (panel) {
          panel.hidden = panel.getAttribute('data-panel') !== name;
        });
        if (title) {
          title.textContent = title.getAttribute('data-title-' + name);
          announce(title.textContent);
        }
        hideResults();
        var focus = name === 'playlist' ? $('[data-panel="playlist"] .file-drop') : $('#q');
        if (focus) focus.focus();
      });
    });
  }

  /* ------------------------------------------------------ Single search */

  function initSearch() {
    var form = $('#search-form');
    var result = $('#search-result');
    if (!form || !result) return;

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var song = parseQuery($('#q').value.trim());
      var formats = checkedValues('format');
      if (!formats.length) {
        toast('Pick at least one format', 'warning');
        return;
      }
      var infos = formats.map(function (f) { return FORMAT_INFO[f]; });
      var bind = function (key, value) {
        var el = $('[data-bind="' + key + '"]', result);
        if (el) el.textContent = value;
      };
      bind('title', song.title);
      bind('artist', song.artist);
      bind('format', formats.join(' + '));
      bind('quality', formats.length === 1 ? infos[0].quality : formats.map(function (f, i) { return f + ' ' + infos[i].quality; }).join(' · '));
      bind('size', sumSizes(infos.map(function (i) { return i.size; })));
      bind('filename', infos.map(function (i) { return song.artist + ' - ' + song.title + '.' + i.ext; }).join(', '));
      bind('dest', 'downloads/' + subfolder());
      result.hidden = false;
      announce('Found ' + song.title + ' by ' + song.artist + ', ' + formats.join(' and ') + '.');
    });

    result.addEventListener('click', function (event) {
      var button = event.target.closest('[data-action]');
      if (!button) return;
      var title = $('[data-bind="title"]', result).textContent;
      var artist = $('[data-bind="artist"]', result).textContent;
      var action = button.getAttribute('data-action');
      if (action === 'download-one') {
        toast('Downloading "' + title + '"', 'success');
      } else if (action === 'queue-one') {
        var formats = checkedValues('format');
        if (!formats.length) { toast('Pick at least one format', 'warning'); return; }
        formats.forEach(function (f) {
          queueSong({ title: title, artist: artist, format: f, size: FORMAT_INFO[f].size });
        });
        toast('Queued "' + title + '"', 'success');
      }
    });
  }

  /* ------------------------------------------------------ Playlist scan */

  function initScan() {
    var button = $('#scan-button');
    if (!button) return;
    var input = $('#csv-input');
    var loading = $('#scan-loading');
    var results = $('#scan-results');
    var rows = $('#scan-rows');
    var server = page === 'server';
    var found = PLAYLIST.filter(function (s) { return s.found; });

    button.addEventListener('click', function () {
      var file = input && input.files && input.files[0];
      var format = checkedValues('csv-format').join(' + ');
      if (!format) {
        toast('Pick at least one format', 'warning');
        return;
      }

      if (button.getAttribute('aria-disabled') === 'true') return;

      results.hidden = true;
      loading.hidden = false;
      /* aria-disabled, not disabled: a disabled button drops keyboard focus. */
      button.setAttribute('aria-disabled', 'true');
      announce('Scanning playlist');

      setTimeout(function () {
        loading.hidden = true;
        button.removeAttribute('aria-disabled');
        $('[data-scan-name]', results).textContent = file ? file.name : 'road-trip-2026.csv';
        $('[data-scan-summary]', results).innerHTML =
          '<strong class="text-success">' + found.length + ' found</strong> · ' +
          (PLAYLIST.length - found.length) + ' not found · ' + format +
          (server ? ' · <span class="text-mono">downloads/' + escapeHtml(subfolder()) + '</span>' : '');

        rows.innerHTML = PLAYLIST.map(function (song, i) {
          var status = song.found
            ? '<span class="tag tag-success">Found</span>'
            : '<span class="tag tag-danger">Not found</span>';
          var action = server ? 'queue-row' : 'download-row';
          var icon = server ? 'playlist_add' : 'download';
          var label = (server ? 'Queue ' : 'Download ') + song.title;
          return '<tr class="' + (song.found ? '' : 'is-missing') + '" data-index="' + i + '">' +
            '<td class="table-numeric col-index">' + (i + 1) + '</td>' +
            '<td><div class="song-cell"><span class="text-medium">' + escapeHtml(song.title) + '</span>' +
              '<span class="text-caption text-muted">' + escapeHtml(song.artist) + '</span></div></td>' +
            '<td>' + status + '</td>' +
            '<td class="table-numeric hide-sm text-muted">' + (song.size || '—') + '</td>' +
            '<td class="col-action"><button class="button button-icon button-icon-sm" type="button" data-action="' + action + '"' +
              ' aria-label="' + escapeHtml(label) + '" title="' + escapeHtml(label) + '"' + (song.found ? '' : ' disabled') + '>' +
              '<span class="icon icon-sm">' + icon + '</span></button></td>' +
            '</tr>';
        }).join('');
        results.hidden = false;
        announce('Scan complete. ' + found.length + ' songs found, ' + (PLAYLIST.length - found.length) + ' not found.');
      }, 900);
    });

    results.addEventListener('click', function (event) {
      var target = event.target.closest('[data-action]');
      if (!target || target.disabled || target.getAttribute('aria-disabled') === 'true') return;
      var action = target.getAttribute('data-action');
      var formats = checkedValues('csv-format');
      if (!formats.length) {
        toast('Pick at least one format', 'warning');
        return;
      }

      if (action === 'download-all') {
        toast('Downloading ' + found.length + ' songs', 'success');
      } else if (action === 'queue-all') {
        found.forEach(function (s) {
          formats.forEach(function (f) { queueSong({ title: s.title, artist: s.artist, format: f, size: s.size }); });
        });
        toast('Queued ' + found.length * formats.length + ' downloads', 'success');
      } else {
        var song = PLAYLIST[+target.closest('tr').getAttribute('data-index')];
        if (action === 'download-row') {
          toast('Downloading "' + song.title + '"', 'success');
        } else {
          formats.forEach(function (f) { queueSong({ title: song.title, artist: song.artist, format: f, size: song.size }); });
          /* Stay focusable (aria-disabled) so focus is not dropped. */
          target.setAttribute('aria-disabled', 'true');
          var queuedLabel = 'Queued ' + song.title;
          target.setAttribute('aria-label', queuedLabel);
          target.setAttribute('title', queuedLabel);
          target.innerHTML = '<span class="icon icon-sm text-success" aria-hidden="true">check</span>';
          toast('Queued "' + song.title + '"', 'success');
        }
      }
    });
  }

  /* -------------------------------------------------------- Queue rows
     One row renderer for both the server page's "Your queue" and the All
     page. The tbody's data-columns says which cells it has. Status is its
     own column; progress sits in the next one, the bar with its percentage
     beside it. */

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
      return '<div class="progress-cell">' +
        '<div class="progress-linear' + (status === 'paused' ? ' progress-paused' : '') + '" role="progressbar"' +
          ' aria-valuenow="' + value + '" aria-valuemin="0" aria-valuemax="100"' +
          ' aria-valuetext="' + progressText(row, value) + '" aria-label="Progress of ' + escapeHtml(rowName(row)) + '">' +
          '<div class="progress-bar" style="--value: ' + value + '%;"></div></div>' +
        '<span class="text-caption text-muted progress-value" aria-hidden="true">' + value + '%</span>' +
      '</div>';
    }
    var note = {
      queued: 'Waiting',
      cancelled: '—',
      failed: row.getAttribute('data-note') || 'Error',
      done: row.getAttribute('data-size') || ''
    }[status];
    return '<span class="text-caption text-muted">' + escapeHtml(note) + '</span>';
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

  function renderRow(row) {
    var cell;
    if ((cell = $('.cell-status', row))) cell.innerHTML = STATUS_TAG[row.getAttribute('data-status')];
    if ((cell = $('.cell-progress', row))) cell.innerHTML = progressHtml(row);
    if ((cell = $('.row-actions', row))) cell.innerHTML = actionsHtml(row);
  }

  function buildRow(item, columns) {
    var row = document.createElement('tr');
    row.setAttribute('data-status', item.status);
    row.setAttribute('data-progress', item.progress || 0);
    if (item.size) row.setAttribute('data-size', item.size);
    if (item.note) row.setAttribute('data-note', item.note);
    row.innerHTML = columns.map(function (col) {
      switch (col) {
        case 'song':
          return '<td class="cell-song"><div class="song-cell"><span class="text-medium" data-name>' + escapeHtml(item.title) + '</span>' +
            '<span class="text-caption text-muted">' + escapeHtml(item.artist + ' · ' + item.format) + '</span></div></td>';
        case 'folder':   return '<td class="hide-sm text-mono">' + escapeHtml(item.folder) + '</td>';
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

  function setStatus(row, status) {
    row.setAttribute('data-status', status);
    renderRow(row);
  }

  /* Adds a song from the server page's search or scan to "Your queue",
     after the active rows and ahead of anything finished. */
  function queueSong(song) {
    var tbody = $('#queue-rows');
    if (!tbody) return;
    var columns = tbody.getAttribute('data-columns').split(' ');
    var row = buildRow({
      title: song.title, artist: song.artist, format: song.format, size: song.size,
      folder: subfolder(), by: 'admin', status: 'queued'
    }, columns);
    tbody.insertBefore(row, $('tr[data-status="done"], tr[data-status="failed"], tr[data-status="cancelled"]', tbody));
  }

  /* Nudges every downloading row forward so the queue looks alive. */
  function tick(tbody) {
    $$('tr[data-status="downloading"]', tbody).forEach(function (row) {
      var value = Math.min(100, +row.getAttribute('data-progress') + 1 + Math.floor(Math.random() * 3));
      row.setAttribute('data-progress', value);
      if (value >= 100) {
        setStatus(row, 'done');
        var next = $('tr[data-status="queued"]', tbody);
        if (next) setStatus(next, 'downloading');
        return;
      }
      var bar = $('.progress-linear', row);
      bar.setAttribute('aria-valuenow', value);
      bar.setAttribute('aria-valuetext', progressText(row, value));
      $('.progress-bar', bar).style.setProperty('--value', value + '%');
      $('.progress-value', row).textContent = value + '%';
    });
  }

  /* ------------------------------------------------ Server: your queue */

  function initMyQueue() {
    if (page !== 'server') return;
    var tbody = $('#queue-rows');
    var columns = tbody.getAttribute('data-columns').split(' ');
    QUEUE.filter(function (item) { return item.by === 'admin'; }).forEach(function (item) {
      tbody.appendChild(buildRow(item, columns));
    });
    setInterval(function () { tick(tbody); }, 1200);
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

    QUEUE.forEach(function (item) { tbody.appendChild(buildRow(item, columns)); });

    function nameOf(row) { return $('[data-name]', row).textContent; }

    function refresh() {
      var term = (search.value || '').trim().toLowerCase();
      var rows = $$('tr', tbody);
      var visible = 0;
      var counts = { downloading: 0, queued: 0, stopped: 0, done: 0 };
      rows.forEach(function (row) {
        var status = row.getAttribute('data-status');
        if (STOPPED.indexOf(status) > -1) counts.stopped++; else counts[status]++;
        var show = FILTERS[filter](status) && (!term || row.textContent.toLowerCase().indexOf(term) > -1);
        row.hidden = !show;
        if (show) visible++;
      });
      Object.keys(counts).forEach(function (key) {
        setText($('[data-count="' + key + '"]'), counts[key]);
      });
      $$('.badge-count, .nav-item-trailing').forEach(function (el) {
        setText(el, counts.downloading + counts.queued);
      });
      $('#queue-empty').hidden = visible > 0;
      setText($('#queue-summary'), visible + ' of ' + rows.length + ' items');
    }

    $('#status-filter').addEventListener('click', function (event) {
      var button = event.target.closest('[data-filter]');
      if (!button) return;
      filter = button.getAttribute('data-filter');
      refresh();
    });
    search.addEventListener('input', refresh);

    tbody.addEventListener('click', function (event) {
      var button = event.target.closest('[data-action]');
      if (!button) return;
      var row = button.closest('tr');
      var name = nameOf(row);
      var action = button.getAttribute('data-action');
      /* The buttons are rebuilt when the status changes, which would drop
         keyboard focus on the page. Put it on the row's first action. */
      var keepFocus = action !== 'delete';
      if (action === 'pause') {
        setStatus(row, 'paused');
        toast('Paused "' + name + '"');
      } else if (action === 'resume') {
        setStatus(row, 'downloading');
        toast('Resumed "' + name + '"');
      } else if (action === 'cancel') {
        setStatus(row, 'cancelled');
        toast('Cancelled "' + name + '"');
      } else if (action === 'delete') {
        pendingDelete = row;
        $('#dlg-delete-name').textContent = name;
        UI.openDialog('dlg-delete');
      }
      refresh();
      if (keepFocus) {
        var next = $('.row-actions [data-action]', row);
        if (next) next.focus();
      }
    });

    dialog.addEventListener('close', function () {
      if (dialog.returnValue === 'delete' && pendingDelete) {
        var name = nameOf(pendingDelete);
        var sibling = pendingDelete.nextElementSibling;
        while (sibling && sibling.hidden) sibling = sibling.nextElementSibling;
        pendingDelete.remove();
        toast('Deleted "' + name + '"', 'success');
        refresh();
        var target = sibling && $('.row-actions [data-action]', sibling);
        (target || search).focus();
      }
      pendingDelete = null;
    });

    $('[data-action="pause-all"]').addEventListener('click', function () {
      $$('tr[data-status="downloading"], tr[data-status="queued"]', tbody).forEach(function (row) {
        setStatus(row, 'paused');
      });
      toast('Paused everything');
      refresh();
    });
    $('[data-action="clear-done"]').addEventListener('click', function () {
      var done = $$('tr[data-status="done"]', tbody);
      done.forEach(function (row) { row.remove(); });
      toast('Cleared ' + done.length + ' finished');
      refresh();
    });

    setInterval(function () { tick(tbody); refresh(); }, 1200);
    refresh();
  }

  /* ------------------------------------------------------------ Login */

  function initLogin() {
    var form = $('#login-form');
    if (!form) return;
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      window.location.href = 'server.html';
    });
  }

  initPanels();
  initSearch();
  initScan();
  initMyQueue();
  initAll();
  initLogin();
})();
