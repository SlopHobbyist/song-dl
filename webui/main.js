/* Song-DL mockup.
   Nothing here talks to a server. Every button swaps in canned data so the
   flows can be clicked through; the kit (design_language.js) supplies the
   tabs, drawer, dialogs, toasts and file drop. */
(function () {
  'use strict';

  var page = document.body.getAttribute('data-page');

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function toast(message, variant) {
    if (window.UI && UI.toast) UI.toast(message, { variant: variant });
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function checkedValue(name) {
    var input = $('input[name="' + name + '"]:checked');
    return input ? input.value : 'FLAC';
  }

  function subfolder() {
    var field = $('#subfolder');
    var value = field ? field.value.trim().replace(/^\/+|\/+$/g, '') : '';
    return 'downloads/' + (value ? value + '/' : '');
  }

  /* -------------------------------------------------------- Canned data */

  var FORMAT_INFO = {
    FLAC: { quality: '24-bit / 96 kHz', size: '48.6 MB', ext: 'flac' },
    WAV:  { quality: '16-bit / 44.1 kHz', size: '44.4 MB', ext: 'wav' }
  };

  var PLAYLIST = [
    { title: 'Paper Lanterns',    artist: 'The Quiet Harbor', size: '48.6 MB', found: true },
    { title: 'Glasswing',         artist: 'Northbound Static', size: '50.1 MB', found: true },
    { title: 'Copper Season',     artist: 'Marlow Fields',    size: '41.2 MB', found: true },
    { title: 'Small Hours',       artist: 'Juniper Avenue',   size: '37.9 MB', found: true },
    { title: 'Neon Orchard',      artist: 'Vela Coast',       size: null,      found: false },
    { title: 'Rooftop Choir',     artist: 'Bellweather',      size: '39.2 MB', found: true },
    { title: 'Morning Ferry',     artist: 'Bellweather',      size: '54.1 MB', found: true },
    { title: 'Salt & Cedar',      artist: 'Ida Fontaine',     size: null,      found: false },
    { title: 'Weather Vane',      artist: 'Ottoline',         size: '46.0 MB', found: true },
    { title: 'Signal Fires',      artist: 'The Dunmore Set',  size: '43.3 MB', found: true },
    { title: 'Last Train West',   artist: 'Harrow & Pine',    size: null,      found: false },
    { title: 'Lighthouse Keeper', artist: 'Ottoline',         size: '52.7 MB', found: true }
  ];

  /* "Title - Artist" or "Title by Artist"; anything else is just a title. */
  function parseQuery(query) {
    var parts = query.split(/\s+(?:-|–|by)\s+/i);
    if (parts.length >= 2) return { title: parts[0], artist: parts.slice(1).join(' ') };
    return { title: query || 'Paper Lanterns', artist: query ? 'Unknown artist' : 'The Quiet Harbor' };
  }

  /* ------------------------------------------------------ Single search */

  function initSearch() {
    var form = $('#search-form');
    var result = $('#search-result');
    if (!form || !result) return;

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var song = parseQuery($('#q').value.trim());
      var format = checkedValue('format');
      var info = FORMAT_INFO[format];
      var bind = function (key, value) {
        var el = $('[data-bind="' + key + '"]', result);
        if (el) el.textContent = value;
      };
      bind('title', song.title);
      bind('artist', song.artist);
      bind('format', format);
      bind('quality', info.quality);
      bind('size', info.size);
      bind('filename', song.artist + ' - ' + song.title + '.' + info.ext);
      bind('dest', subfolder());
      result.hidden = false;
      result.classList.remove('is-shown');
      void result.offsetWidth;
      result.classList.add('is-shown');
    });

    result.addEventListener('click', function (event) {
      var button = event.target.closest('[data-action]');
      if (!button) return;
      var title = $('[data-bind="title"]', result).textContent;
      var action = button.getAttribute('data-action');
      if (action === 'clear-search') {
        result.hidden = true;
        $('#q').value = '';
        $('#q').focus();
      } else if (action === 'download-one') {
        toast('Mockup: "' + title + '" would download to your computer', 'success');
      } else if (action === 'queue-one') {
        addToMyQueue(title, $('[data-bind="artist"]', result).textContent, checkedValue('format'));
        toast('Queued "' + title + '" to ' + subfolder(), 'success');
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

    button.addEventListener('click', function () {
      var file = input && input.files && input.files[0];
      var name = file ? file.name : 'road-trip-2026.csv';
      var format = checkedValue('csv-format');
      var found = PLAYLIST.filter(function (s) { return s.found; }).length;

      results.hidden = true;
      loading.hidden = false;
      button.disabled = true;

      setTimeout(function () {
        loading.hidden = true;
        button.disabled = false;
        $('.table-toolbar .text-heading-3', results).textContent = name;
        $('.table-toolbar .text-caption', results).innerHTML =
          '<strong class="text-success">' + found + ' found</strong> · ' +
          (PLAYLIST.length - found) + ' not on the server · ' + format +
          (server ? ' · to <span class="text-mono">' + escapeHtml(subfolder()) + '</span>' : '');

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
              ' aria-label="' + escapeHtml(label) + '"' + (song.found ? '' : ' disabled') + '>' +
              '<span class="icon icon-sm">' + icon + '</span></button></td>' +
            '</tr>';
        }).join('');
        results.hidden = false;
      }, 900);
    });

    results.addEventListener('click', function (event) {
      var target = event.target.closest('[data-action]');
      if (!target || target.disabled) return;
      var action = target.getAttribute('data-action');
      var format = checkedValue('csv-format');
      var found = PLAYLIST.filter(function (s) { return s.found; });

      if (action === 'download-all') {
        toast('Mockup: ' + found.length + ' files would download as a zip', 'success');
      } else if (action === 'queue-all') {
        found.forEach(function (s) { addToMyQueue(s.title, s.artist, format); });
        toast('Queued ' + found.length + ' songs to ' + subfolder(), 'success');
      } else {
        var song = PLAYLIST[+target.closest('tr').getAttribute('data-index')];
        if (action === 'download-row') {
          toast('Mockup: "' + song.title + '" would download to your computer', 'success');
        } else {
          addToMyQueue(song.title, song.artist, format);
          target.disabled = true;
          target.innerHTML = '<span class="icon icon-sm text-success">check</span>';
          toast('Queued "' + song.title + '"', 'success');
        }
      }
    });
  }

  /* ------------------------------------------------ Server: your queue */

  function addToMyQueue(title, artist, format) {
    var list = $('#my-queue');
    if (!list) return;
    var item = document.createElement('div');
    item.className = 'queue-item is-new';
    item.setAttribute('data-status', 'queued');
    item.innerHTML =
      '<span class="icon queue-icon">schedule</span>' +
      '<div class="queue-main">' +
        '<div class="row justify-between row-nowrap">' +
          '<span class="text-subtitle-2 text-truncate">' + escapeHtml(artist + ' — ' + title) + '</span>' +
          '<span class="tag">Queued</span>' +
        '</div>' +
        '<p class="text-caption text-muted mt-1 text-mono">' + escapeHtml(subfolder()) + ' · ' + escapeHtml(format) + '</p>' +
      '</div>';
    /* New items join the end of the queue, ahead of anything already finished. */
    list.insertBefore(item, $('.queue-item[data-status="done"]', list));
  }

  /* Nudges every downloading bar forward so the queue looks alive. */
  function tickProgress(root, onDone) {
    $$('[data-status="downloading"]', root).forEach(function (row) {
      var bar = $('.progress-linear', row);
      if (!bar) return;
      var value = Math.min(100, +bar.getAttribute('aria-valuenow') + 1 + Math.floor(Math.random() * 3));
      bar.setAttribute('aria-valuenow', value);
      $('.progress-bar', bar).style.setProperty('--value', value + '%');
      var label = $('[data-progress-label]', row);
      if (label) label.textContent = value + '%';
      if (value >= 100 && onDone) onDone(row);
    });
  }

  function initMyQueue() {
    var list = $('#my-queue');
    if (!list) return;
    setInterval(function () {
      tickProgress(list, function (item) {
        item.setAttribute('data-status', 'done');
        var icon = $('.queue-icon', item);
        icon.textContent = 'check_circle';
        icon.classList.add('text-success');
        var bar = $('.progress-linear', item);
        bar.remove();
        $('[data-progress-label]', item).outerHTML = '<span class="tag tag-success">Done</span>';
        startNextQueued(list);
      });
    }, 1200);
  }

  function startNextQueued(list) {
    var next = $('.queue-item[data-status="queued"]', list);
    if (!next) return;
    next.setAttribute('data-status', 'downloading');
    $('.queue-icon', next).textContent = 'downloading';
    var tag = $('.tag', next);
    var name = $('.text-subtitle-2', next).textContent;
    tag.outerHTML = '<span class="text-caption text-muted text-nowrap" data-progress-label>0%</span>';
    $('.row', next).insertAdjacentHTML('afterend',
      '<div class="progress-linear mt-2" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100" aria-label="' +
      escapeHtml(name) + ' progress"><div class="progress-bar" style="--value: 0%;"></div></div>');
  }

  /* --------------------------------------------------------- All page */

  var STOPPED = ['paused', 'cancelled', 'failed'];
  var FILTERS = {
    all: function () { return true; },
    active: function (s) { return s === 'downloading' || s === 'queued'; },
    stopped: function (s) { return STOPPED.indexOf(s) > -1; },
    done: function (s) { return s === 'done'; }
  };

  function actionButton(action, icon, label) {
    return '<button class="button button-icon button-icon-sm" type="button" data-action="' + action +
      '" aria-label="' + label + '" title="' + label + '"><span class="icon icon-sm">' + icon + '</span></button>';
  }

  function renderRowActions(row) {
    var status = row.getAttribute('data-status');
    var html = '';
    if (status === 'downloading' || status === 'queued') html += actionButton('pause', 'pause', 'Pause');
    if (status === 'paused' || status === 'cancelled') html += actionButton('resume', 'play_arrow', 'Resume');
    if (status === 'failed') html += actionButton('resume', 'refresh', 'Retry');
    if (status === 'downloading' || status === 'queued' || status === 'paused') html += actionButton('cancel', 'block', 'Cancel');
    html += actionButton('delete', 'delete', 'Delete');
    $('.row-actions', row).innerHTML = html;
  }

  function progressValue(row) {
    var bar = $('.progress-linear', row);
    return bar ? +bar.getAttribute('aria-valuenow') : 0;
  }

  function setRowStatus(row, status) {
    var name = $('.song-cell .text-medium', row).textContent;
    var value = progressValue(row);
    var cell = $('.status-cell', row);
    var bar = function (extra) {
      return '<div class="progress-linear' + (extra || '') + '" role="progressbar" aria-valuenow="' + value +
        '" aria-valuemin="0" aria-valuemax="100" aria-label="' + escapeHtml(name) + ' progress">' +
        '<div class="progress-bar" style="--value: ' + value + '%;"></div></div>';
    };
    var head = function (tag) {
      return '<div class="row justify-between row-nowrap">' + tag +
        '<span class="text-caption text-muted" data-progress-label>' + value + '%</span></div>';
    };
    var html = {
      downloading: head('<span class="tag tag-info">Downloading</span>') + bar(),
      queued:      '<span class="tag">Queued</span>',
      paused:      head('<span class="tag tag-warning">Paused</span>') + bar(' progress-paused'),
      cancelled:   '<span class="tag tag-danger">Cancelled</span><span class="text-caption text-muted">Stopped by admin</span>',
      done:        '<span class="tag tag-success">Done</span><span class="text-caption text-muted">just now</span>'
    }[status];
    row.setAttribute('data-status', status);
    cell.innerHTML = html;
    renderRowActions(row);
  }

  function initAll() {
    var tbody = $('#queue-rows');
    if (!tbody) return;
    var filter = 'all';
    var search = $('#queue-search');
    var pendingDelete = null;
    var dialog = $('#dlg-delete');

    function refresh() {
      var term = (search.value || '').trim().toLowerCase();
      var visible = 0;
      var counts = { downloading: 0, queued: 0, stopped: 0, done: 0 };
      $$('tr', tbody).forEach(function (row) {
        var status = row.getAttribute('data-status');
        if (STOPPED.indexOf(status) > -1) counts.stopped++; else counts[status]++;
        var show = FILTERS[filter](status) && (!term || row.textContent.toLowerCase().indexOf(term) > -1);
        row.hidden = !show;
        if (show) visible++;
      });
      Object.keys(counts).forEach(function (key) {
        var el = $('[data-count="' + key + '"]');
        if (el) el.textContent = counts[key];
      });
      $$('.badge-count, .nav-item-trailing').forEach(function (el) {
        el.textContent = counts.downloading + counts.queued;
      });
      $('#queue-empty').hidden = visible > 0;
      $('#queue-summary').textContent = visible + ' of ' + $$('tr', tbody).length + ' items';
    }

    $$('tr', tbody).forEach(renderRowActions);

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
      var name = $('.song-cell .text-medium', row).textContent;
      var action = button.getAttribute('data-action');
      if (action === 'pause') {
        setRowStatus(row, 'paused');
        toast('Paused "' + name + '"');
      } else if (action === 'resume') {
        setRowStatus(row, 'downloading');
        toast('Resumed "' + name + '"');
      } else if (action === 'cancel') {
        setRowStatus(row, 'cancelled');
        toast('Cancelled "' + name + '"');
      } else if (action === 'delete') {
        pendingDelete = row;
        $('#dlg-delete-name').textContent = name;
        UI.openDialog('dlg-delete');
      }
      refresh();
    });

    dialog.addEventListener('close', function () {
      if (dialog.returnValue === 'delete' && pendingDelete) {
        var name = $('.song-cell .text-medium', pendingDelete).textContent;
        pendingDelete.remove();
        toast('Deleted "' + name + '"', 'success');
        refresh();
      }
      pendingDelete = null;
    });

    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-action="pause-all"], [data-action="clear-done"]');
      if (!button) return;
      if (button.getAttribute('data-action') === 'pause-all') {
        $$('tr[data-status="downloading"], tr[data-status="queued"]', tbody).forEach(function (row) {
          setRowStatus(row, 'paused');
        });
        toast('Paused everything');
      } else {
        var done = $$('tr[data-status="done"]', tbody);
        done.forEach(function (row) { row.remove(); });
        toast('Cleared ' + done.length + ' finished downloads');
      }
      refresh();
    });

    setInterval(function () {
      tickProgress(tbody, function (row) {
        setRowStatus(row, 'done');
        var next = $('tr[data-status="queued"]', tbody);
        if (next) setRowStatus(next, 'downloading');
      });
      refresh();
    }, 1200);

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

  initSearch();
  initScan();
  initMyQueue();
  initAll();
  initLogin();
})();
