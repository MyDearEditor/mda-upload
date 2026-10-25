/* My Dear Academy upload page — talks only to the Apps Script "doorman" (config.js) */
(function () {
  'use strict';
  var CFG = window.MDA_CONFIG;
  var CHUNK = CFG.CHUNK_MB * 1024 * 1024;
  var $ = function (id) { return document.getElementById(id); };
  var session = load('mda_session');

  // ---------------------------------------------------------------- api
  function api(body, _retry) {
    body = Object.assign({}, body);
    if (session && !body.token) body.token = session.token;
    return fetch(CFG.API_URL, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: JSON.stringify(body) })
      .catch(function (err) {   // transient network blip ("Failed to fetch"): retry once after a moment
        if (_retry) throw new Error('Connection problem. Check your signal and try again.');
        return new Promise(function (res) { setTimeout(res, 1500); }).then(function () { return api(body, true); }).then(function (j) { return { json: function () { return j; } }; });
      })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok && j.code === 'AUTH') { signOut(); throw new Error('Your session expired. Please sign in again.'); }
        if (!j.ok) throw new Error(j.error || 'Something went wrong');
        return j;
      });
  }
  function load(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } }
  function save(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

  // ---------------------------------------------------------------- views
  var views = ['gate1', 'join', 'login', 'upload', 'mine', 'admin'];
  function show(v) {
    views.forEach(function (x) { $('view-' + x).classList.toggle('hidden', x !== v); });
    var el = $('view-' + v); el.classList.remove('enter'); void el.offsetWidth; el.classList.add('enter');
    document.body.classList.toggle('gated', ['gate1', 'join', 'login'].indexOf(v) >= 0);
    document.querySelectorAll('.navbtn[data-view]').forEach(function (b) { b.classList.toggle('active', b.dataset.view === v); });
    if (v === 'mine') loadMine();
    if (v === 'admin') { loadAgents(); loadLog(); }
  }
  function applySession() {
    var on = !!(session && session.expires > Date.now());
    if (!on) session = null;
    $('nav').classList.toggle('hidden', !on);
    if (!on) return show('gate1');
    $('who').textContent = session.display_name;
    var admin = !!session.admin;
    $('hud-count').textContent = admin ? 'ADMIN MODE' : 'HEY ' + String(session.display_name).split(' ')[0].toUpperCase();
    if (!admin) api({ action: 'myCalls' }).then(function (j) {
      var week = Date.now() - 7 * 86400000;
      var n = j.calls.filter(function (c) { return new Date(c.uploaded_at).getTime() > week; }).length;
      $('hud-count').textContent = n + ' CALL' + (n === 1 ? '' : 'S') + ' THIS WEEK';
    }).catch(function () {});
    document.querySelectorAll('.admin-only').forEach(function (e) { e.classList.toggle('hidden', !admin); });
    document.querySelector('.navbtn[data-view="upload"]').classList.toggle('hidden', admin);
    document.querySelector('.navbtn[data-view="mine"]').classList.toggle('hidden', admin);
    show(admin ? 'admin' : 'upload');
  }
  function signOut() { session = null; save('mda_session', null); applySession(); }
  document.querySelectorAll('.navbtn[data-view]').forEach(function (b) { b.onclick = function () { show(b.dataset.view); }; });
  document.querySelectorAll('[data-go]').forEach(function (b) { b.onclick = function () { show(b.dataset.go); if (b.dataset.go === 'login') setTimeout(function () { $('login-user').focus(); }, 300); }; });
  $('book-btn').href = CFG.BOOK_URL || '#';
  $('logout').onclick = signOut;

  // ---------------------------------------------------------------- login
  $('login-form').onsubmit = function (e) {
    e.preventDefault();
    $('login-err').textContent = ''; $('login-btn').disabled = true;
    api({ action: 'login', username: $('login-user').value, password: $('login-pass').value })
      .then(function (j) {
        session = { token: j.token, display_name: j.display_name, admin: j.admin, expires: j.expires };
        save('mda_session', session); $('login-pass').value = ''; applySession();
      })
      .catch(function (err) { $('login-err').textContent = err.message; })
      .then(function () { $('login-btn').disabled = false; });
  };

  // ---------------------------------------------------------------- upload: file pick
  var files = { video: null, audio: null };
  ['video', 'audio'].forEach(function (kind) {
    var drop = $('drop-' + kind), input = $('file-' + kind);
    drop.onclick = function () { if (!uploading) input.click(); };
    input.onchange = function () { setFile(kind, input.files[0]); };
    drop.ondragover = function (e) { e.preventDefault(); drop.classList.add('over'); };
    drop.ondragleave = function () { drop.classList.remove('over'); };
    drop.ondrop = function (e) { e.preventDefault(); drop.classList.remove('over'); if (!uploading && e.dataTransfer.files[0]) setFile(kind, e.dataTransfer.files[0]); };
  });
  function setFile(kind, f) {
    files[kind] = f || null;
    $('name-' + kind).textContent = f ? f.name + ' · ' + fmtMB(f.size) : '';
    $('drop-' + kind).classList.toggle('has', !!f);
    $('upload-btn').disabled = !(files.video && files.audio);
  }
  function fmtMB(b) { return b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : Math.round(b / 1e6) + ' MB'; }
  var progress = { video: 0, audio: 0 };
  function setBar(kind, done, total) {
    var p = total ? Math.round(done / total * 100) : 0;
    $('bar-' + kind).style.width = p + '%';
    $('pct-' + kind).textContent = total ? p + '% · ' + fmtMB(done) + ' of ' + fmtMB(total) : '';
    progress[kind] = done;
    var all = (files.video ? files.video.size : 0) + (files.audio ? files.audio.size : 0);
    var got = progress.video + progress.audio;
    $('total-fill').style.width = (all ? Math.min(100, Math.round(got / all * 100)) : 0) + '%';
  }

  // ---------------------------------------------------------------- upload: run
  var uploading = false;
  window.addEventListener('beforeunload', function (e) { if (uploading) { e.preventDefault(); e.returnValue = ''; } });

  $('upload-btn').onclick = function () {
    if (uploading || !files.video || !files.audio) return;
    uploading = true; $('upload-btn').disabled = true; $('upload-err').textContent = '';
    progress.video = progress.audio = 0; $('total-fill').style.width = '0%'; $('total-wrap').classList.remove('hidden');
    var status = function (t) { $('upload-status').textContent = t.toUpperCase(); };
    var callId, ids = {};
    status('Starting…');
    api({ action: 'startCall', note: $('note').value })
      .then(function (j) { callId = j.callId; status('Uploading video…'); return uploadOne('video', files.video, callId); })
      .then(function (id) { ids.video = id; $('drop-video').classList.add('done'); status('Uploading call recording…'); return uploadOne('audio', files.audio, callId); })
      .then(function (id) { ids.audio = id; $('drop-audio').classList.add('done'); status('Finishing…');
        return api({ action: 'finishCall', callId: callId, videoFileId: ids.video, audioFileId: ids.audio }); })
      .then(function () {
        status('DONE!'); $('total-fill').style.width = '100%'; uploading = false;
        // eagle flies across, then the "Got it!" panel pops in
        var eagle = $('eagle'); eagle.classList.remove('idle'); eagle.classList.add('fly');
        setTimeout(function () {
          $('upload-card').classList.add('hidden'); $('upload-done').classList.remove('hidden');
        }, 1400);
        setTimeout(function () { eagle.classList.remove('fly'); eagle.classList.add('idle'); }, 4000);
      })
      .catch(function (err) {
        uploading = false; status('');
        $('upload-err').textContent = err.message + ' — nothing was lost; press Upload to try again.';
        $('upload-btn').disabled = false;
      });
  };
  $('upload-again').onclick = function () {
    ['video', 'audio'].forEach(function (k) { setFile(k, null); setBar(k, 0, 0); $('drop-' + k).classList.remove('done'); $('file-' + k).value = ''; });
    $('note').value = ''; $('total-wrap').classList.add('hidden'); $('upload-status').textContent = '';
    $('upload-done').classList.add('hidden'); $('upload-card').classList.remove('hidden');
  };

  // Chunked, resumable upload straight to Google Drive (session URL comes from the doorman).
  function uploadOne(kind, file, callId) {
    var key = 'mda_up_' + callId + '_' + kind;
    var saved = load(key);
    var start = saved && saved.name === file.name && saved.size === file.size ? Promise.resolve(saved.url) :
      api({ action: 'startUpload', token: session.token, callId: callId, kind: kind, filename: file.name, size: file.size,
            mimeType: file.type || 'application/octet-stream', origin: location.origin })
        .then(function (j) { save(key, { url: j.uploadUrl, name: file.name, size: file.size }); return j.uploadUrl; });

    return start.then(function (url) {
      return queryOffset(url, file.size).then(function (offset) { return sendFrom(url, file, offset, kind, 0); });
    }).then(function (id) { save(key, null); return id; });
  }

  // Ask Google how much of this file it already has (0 on a fresh session).
  function queryOffset(url, size) {
    return fetch(url, { method: 'PUT', headers: { 'Content-Range': 'bytes */' + size } })
      .then(function (r) {
        if (r.status === 308) { var m = /bytes=0-(\d+)/.exec(r.headers.get('Range') || ''); return m ? Number(m[1]) + 1 : 0; }
        if (r.status === 200 || r.status === 201) return size;      // already complete
        if (r.status === 404 || r.status === 410) throw new Error('Upload session expired, starting over');
        return 0;
      })
      .catch(function (err) { if (/expired/.test(err.message)) throw err; return 0; });
  }

  function sendFrom(url, file, offset, kind, retries) {
    var size = file.size;
    if (offset >= size) return finalId(url, size);
    var end = Math.min(offset + CHUNK, size);
    var chunk = file.slice(offset, end);
    return fetch(url, { method: 'PUT', headers: { 'Content-Range': 'bytes ' + offset + '-' + (end - 1) + '/' + size }, body: chunk })
      .then(function (r) {
        if (r.status === 308) {
          var m = /bytes=0-(\d+)/.exec(r.headers.get('Range') || '');
          var next = m ? Number(m[1]) + 1 : end;
          setBar(kind, next, size);
          return sendFrom(url, file, next, kind, 0);
        }
        if (r.status === 200 || r.status === 201) { setBar(kind, size, size); return r.json().then(function (j) { return j.id; }); }
        if (r.status === 404 || r.status === 410) throw new Error('Upload session expired');
        throw new Error('upload error ' + r.status);
      })
      .catch(function (err) {
        if (/expired/.test(err.message) || retries >= 8) throw err;
        var wait = Math.min(30000, 1000 * Math.pow(2, retries));
        $('upload-status').textContent = 'Connection hiccup — resuming in ' + Math.round(wait / 1000) + 's…';
        return new Promise(function (res) { setTimeout(res, wait); })
          .then(function () { return queryOffset(url, size); })
          .then(function (off) { return sendFrom(url, file, off, kind, retries + 1); });
      });
  }
  function finalId(url, size) {
    return fetch(url, { method: 'PUT', headers: { 'Content-Range': 'bytes */' + size } })
      .then(function (r) { return r.json(); }).then(function (j) { if (!j.id) throw new Error('could not confirm upload'); return j.id; });
  }

  // ---------------------------------------------------------------- my uploads
  function loadMine() {
    var el = $('mine-list'); el.innerHTML = '<div class="empty">Loading…</div>';
    api({ action: 'myCalls' }).then(function (j) {
      $('my-folder').href = j.folder_url;
      if (!j.calls.length) { el.innerHTML = '<div class="empty">No uploads yet.</div>'; return; }
      el.innerHTML = j.calls.map(function (c) {
        return '<div class="item"><div class="t">' + esc(fmtDate(c.uploaded_at)) + (c.note ? ' · ' + esc(c.note) : '') + '</div>' +
          '<span class="pill ' + esc(c.status) + '">' + esc(c.status) + (c.status === 'Ready' && c.clips ? ' · ' + c.clips + ' clips' : '') + '</span>' +
          (c.problem ? '<div class="m">' + esc(c.problem) + '</div>' : '') + '</div>';
      }).join('');
    }).catch(function (err) { el.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }

  // ---------------------------------------------------------------- admin
  $('add-form').onsubmit = function (e) {
    e.preventDefault();
    var f = e.target, b = { action: 'adminAddAgent' };
    ['username', 'password', 'display_name', 'email', 'mobile', 'folder_link'].forEach(function (k) { b[k] = f[k].value.trim(); });
    $('add-err').textContent = ''; $('add-ok').textContent = 'Adding…';
    api(b).then(function (j) {
      $('add-ok').textContent = 'Added ' + j.agent.display_name + '. Folder: ' + j.folder_url + ' — now send them the page link, username and password.';
      f.reset(); loadAgents();
    }).catch(function (err) { $('add-ok').textContent = ''; $('add-err').textContent = err.message; });
  };
  $('reload-agents').onclick = loadAgents;
  $('reload-log').onclick = loadLog;

  function loadAgents() {
    var el = $('agents'); el.innerHTML = '<div class="empty">Loading…</div>';
    api({ action: 'adminList' }).then(function (j) {
      if (!j.agents.length) { el.innerHTML = '<div class="empty">No agents yet.</div>'; return; }
      el.innerHTML = j.agents.map(function (a) {
        return '<div class="item"><div><div class="t">' + esc(a.display_name) + ' <span class="pill' + (a.active ? '' : ' off') + '">' + (a.active ? a.username : 'deactivated') + '</span></div>' +
          '<div class="m">' + esc(a.email) + (a.mobile ? ' · ' + esc(a.mobile) : '') + ' · last sign-in ' + esc(fmtDate(a.last_login) || 'never') + '</div></div>' +
          '<div class="actions"><button class="btn small" data-act="reset" data-u="' + esc(a.username) + '">Reset password</button>' +
          '<button class="btn small" data-act="toggle" data-u="' + esc(a.username) + '" data-on="' + (a.active ? 1 : 0) + '">' + (a.active ? 'Deactivate' : 'Reactivate') + '</button>' +
          '<a class="btn small" target="_blank" rel="noopener" href="https://drive.google.com/drive/folders/' + esc(a.folder_id) + '">Folder ↗</a></div></div>';
      }).join('');
      el.querySelectorAll('button[data-act]').forEach(function (btn) {
        btn.onclick = function () {
          var u = btn.dataset.u;
          if (btn.dataset.act === 'reset') {
            var pw = window.prompt('New password for ' + u + ' (8+ characters):');
            if (!pw) return;
            api({ action: 'adminResetPassword', username: u, password: pw }).then(function () { window.alert('Password updated for ' + u); }).catch(function (e) { window.alert(e.message); });
          } else {
            var on = btn.dataset.on === '1';
            if (on && !window.confirm('Deactivate ' + u + '? They will be signed out immediately.')) return;
            api({ action: 'adminSetActive', username: u, active: !on }).then(loadAgents).catch(function (e) { window.alert(e.message); });
          }
        };
      });
    }).catch(function (err) { el.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }
  function loadLog() {
    var el = $('log'); el.innerHTML = '<div class="empty">Loading…</div>';
    api({ action: 'adminLog', limit: 40 }).then(function (j) {
      if (!j.rows.length) { el.innerHTML = '<div class="empty">No uploads yet.</div>'; return; }
      el.innerHTML = j.rows.map(function (r) {
        var st = { started: 'Uploading', uploaded: 'Uploaded', editing: 'Editing', ready: 'Ready', problem: 'Problem' }[r.status] || r.status;
        return '<div class="item"><div><div class="t">' + esc(r.username) + ' · ' + esc(fmtDate(r.uploaded_at)) + '</div>' +
          '<div class="m">' + esc(r.call_id) + ' · video ' + (r.video_MB || 0) + ' MB · audio ' + (r.audio_MB || 0) + ' MB' + (r.note ? ' · ' + esc(r.note) : '') +
          (r.problem ? ' · ' + esc(r.problem) : '') + '</div></div><span class="pill ' + esc(st) + '">' + esc(st) + (r.clips_delivered ? ' · ' + r.clips_delivered + ' clips' : '') + '</span></div>';
      }).join('');
    }).catch(function (err) { el.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }

  // ---------------------------------------------------------------- utils
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function fmtDate(d) { if (!d) return ''; var t = new Date(d); return isNaN(t) ? String(d) : t.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }

  applySession();
})();
