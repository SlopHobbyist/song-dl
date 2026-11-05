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

  /* The file drop is a div acting as a button: give it the keys a button has. */
  $$('.file-drop').forEach(function (zone) {
    zone.addEventListener('keydown', function (event) {
      if (event.target !== zone) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        zone.click();
      }
    });
    /* The kit lists a dropped file but leaves the input empty; fill it in so
       the file can be read. */
    zone.addEventListener('drop', function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      var input = $('input[type="file"]', zone);
      if (!files || !files.length || !input) return;
      try { input.files = files; } catch (e) { return; }
      announce(files[0].name + ' selected');
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

  /* ------------------------------------------------------------ Helpers */

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  /* Every ticked format checkbox in the group, in page order. */
  function checkedValues(name) {
    return $$('input[name="' + name + '"]:checked').map(function (input) { return input.value; });
  }

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
        /* Signed in? The admin button on the home page goes to the Server page. */
        if (!admin || !session.user) return;
        admin.setAttribute('href', '/server/');
        admin.setAttribute('aria-label', 'Server');
        admin.setAttribute('title', 'Server');
        $('.icon', admin).textContent = 'dns';
      }, function () {});
    }
  }

  function setActiveCount(count) {
    $$('.badge-count, .nav-item-trailing').forEach(function (el) { setText(el, count); });
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
     onUpdate(finished, total, progress 0..1) is called as it goes. */
  async function fetchFiles(resultIds, concurrency, onUpdate) {
    var jobs = new Array(resultIds.length);
    var progress = resultIds.map(function () { return 0; });
    var next = 0;
    var finished = 0;

    function report() {
      if (!onUpdate) return;
      var total = progress.reduce(function (sum, p) { return sum + p; }, 0);
      onUpdate(finished, resultIds.length, total / resultIds.length);
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
            progress[i] = view.progress || 0;
            report();
            if (view.state !== 'running') break;
            await sleep(1000);
          }
          view.id = started.id;
          jobs[i] = view;
        } catch (e) {
          jobs[i] = { state: 'failed', error: e.message };
        }
        progress[i] = 1;
        finished++;
        report();
      }
    }

    var workers = [];
    for (var w = 0; w < Math.min(concurrency, resultIds.length); w++) workers.push(worker());
    await Promise.all(workers);
    return jobs;
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
      var query = done.map(function (job) { return 'job=' + encodeURIComponent(job.id); }).join('&');
      saveFile('/api/zip?' + query + '&name=' + encodeURIComponent(zipName));
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

      /* Formats that weren't found, and copies that may be another version */
      var tags = $('.row.row-tight', result);
      $$('.result-extra', tags).forEach(function (tag) { tag.remove(); });
      var notes = job.missing.map(function (f) { return 'No ' + f; });
      found.forEach(function (r) {
        if (r.warning && notes.indexOf(r.warning) < 0) notes.push(r.warning);
      });
      notes.forEach(function (text) {
        var tag = document.createElement('span');
        tag.className = 'tag tag-warning result-extra';
        tag.textContent = text;
        tags.appendChild(tag);
      });
      result.hidden = false;
      var message = 'Found ' + (job.title || job.query) + (job.artist ? ' by ' + job.artist : '') + ', ' +
        found.map(function (r) { return r.format; }).join(' and ') + '.' +
        (job.missing.length ? ' Not found: ' + job.missing.join(', ') + '.' : '');
      announce(message);
    }

    function showEmpty(job) {
      var what = job.missing.join(', ');
      setText($('[data-bind="message"]', empty),
        'Nobody on Soulseek is sharing ' + what + ' files matching that. Soulseek blocks searches for some artists, so try just the song title.');
      empty.hidden = false;
      announce('No match found for ' + job.query);
    }

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (isBusy(submit)) return;
      var query = $('#q').value.trim();
      var formats = checkedValues('format');
      if (!formats.length) {
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
      status.textContent = 'Searching Soulseek';
      loading.hidden = false;
      announce('Searching for ' + query);
      try {
        var started = await api('POST', '/api/search', { query: query, formats: formats });
        var job;
        for (;;) {
          await sleep(1000);
          job = await api('GET', '/api/search/' + started.id);
          if (run !== runs.search) return;
          status.textContent = job.status || 'Searching Soulseek';
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
        setBusy(button, 'Fetching');
        try {
          var jobs = await fetchFiles(ids, ids.length, function (done, total, progress) {
            setBusy(button, progress > 0 ? 'Fetching ' + Math.round(progress * 100) + '%' : 'Fetching');
          });
          await deliver(jobs, '"' + title + '"');
        } finally {
          setIdle(button);
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
    found: '<span class="tag tag-success">Found</span>',
    missing: '<span class="tag tag-danger">Not found</span>'
  };

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
        var signature = song.state + ':' + song.results.length;
        if (!row || row.getAttribute('data-state') === signature) return;
        row.setAttribute('data-state', signature);
        row.classList.toggle('is-missing', song.state === 'missing');
        $('.cell-status', row).innerHTML = SCAN_TAGS[song.state];
        setText($('.cell-size', row), song.size ? formatSize(song.size) : '—');
        $('[data-action]', row).disabled = song.state !== 'found';
      });
    }

    function updateSummary(job) {
      var found = job.tracks.filter(function (t) { return t.state === 'found'; }).length;
      var missing = job.tracks.filter(function (t) { return t.state === 'missing'; }).length;
      var left = job.total - found - missing;
      var parts = ['<strong class="text-success">' + found + ' found</strong>', missing + ' not found'];
      if (job.state === 'running') parts.push(left + ' left to scan');
      parts.push(escapeHtml(job.formats.join(' + ')));
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
      var file = input && input.files && input.files[0];
      var formats = checkedValues('csv-format');
      if (!file) {
        toast('Choose a playlist CSV first', 'warning');
        return;
      }
      if (!formats.length) {
        toast('Pick at least one format', 'warning');
        return;
      }
      if (file.size > 500 * 1024) {
        toast('That file is too big. A playlist CSV should be well under 500 KB.', 'warning');
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
          '&formats=' + encodeURIComponent(formats.join(',')), undefined, await file.arrayBuffer());
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
          var jobs = await fetchFiles(all, 3, function (done, total) {
            setBusy(target, 'Fetching ' + done + ' of ' + total + ' files');
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
            ok = await deliver(await fetchFiles(ids, ids.length), label);
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
          setIdle(target);
          updateSummary(scan.job);
        }
      }
    });
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
      var data = await api('GET', '/api/queue');
      setActiveCount(data.active);
      syncRows(tbody, data.items.filter(function (item) { return item.by === data.me; }), columns);
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
  initPanels();
  initSearch();
  initScan();
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
