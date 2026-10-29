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


  // ---------------------------------------------------------------- sound (synthesized, 8-bit style)
  var AC = null;
  function audio() {
    try { if (!AC) AC = new (window.AudioContext || window.webkitAudioContext)(); if (AC.state === 'suspended') AC.resume(); } catch (e) { AC = null; }
    return AC;
  }
  document.addEventListener('pointerdown', function () { audio(); }, { once: true });
  function screech() {   // eagle cry: two sharp descending sweeps with a touch of noise
    var ac = audio(); if (!ac) return;
    var t = ac.currentTime;
    [[0, 0.38, 2600, 900], [0.42, 0.3, 2300, 1100]].forEach(function (s) {
      var o = ac.createOscillator(), g = ac.createGain(), f = ac.createBiquadFilter();
      o.type = 'sawtooth'; f.type = 'bandpass'; f.frequency.value = 1800; f.Q.value = 4;
      o.frequency.setValueAtTime(s[2], t + s[0]); o.frequency.exponentialRampToValueAtTime(s[3], t + s[0] + s[1]);
      g.gain.setValueAtTime(0.0001, t + s[0]); g.gain.exponentialRampToValueAtTime(0.35, t + s[0] + 0.03); g.gain.exponentialRampToValueAtTime(0.0001, t + s[0] + s[1]);
      o.connect(f); f.connect(g); g.connect(ac.destination); o.start(t + s[0]); o.stop(t + s[0] + s[1] + 0.05);
    });
  }
  function jingle() {    // level-complete arpeggio
    var ac = audio(); if (!ac) return;
    var t = ac.currentTime, notes = [523, 659, 784, 1047, 784, 1047, 1319];
    notes.forEach(function (fq, i) {
      var o = ac.createOscillator(), g = ac.createGain(); o.type = 'square'; o.frequency.value = fq;
      var s = t + i * 0.09, d = i === notes.length - 1 ? 0.5 : 0.12;
      g.gain.setValueAtTime(0.0001, s); g.gain.exponentialRampToValueAtTime(0.18, s + 0.01); g.gain.exponentialRampToValueAtTime(0.0001, s + d);
      o.connect(g); g.connect(ac.destination); o.start(s); o.stop(s + d + 0.02);
    });
  }
  function blip() {      // tiny tick for progress milestones
    var ac = audio(); if (!ac) return;
    var t = ac.currentTime, o = ac.createOscillator(), g = ac.createGain(); o.type = 'square'; o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.08, t + 0.005); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.06);
    o.connect(g); g.connect(ac.destination); o.start(t); o.stop(t + 0.07);
  }

  // ---------------------------------------------------------------- ambient nature (wind, leaves, birds) — synthesized, loops forever
  var ambient = null, muted = load('mda_muted') === true;
  function noiseBuffer(ac, seconds) {
    var n = ac.sampleRate * seconds, buf = ac.createBuffer(1, n, ac.sampleRate), d = buf.getChannelData(0), b0 = 0, b1 = 0, b2 = 0;
    for (var i = 0; i < n; i++) {   // pink-ish noise
      var w = Math.random() * 2 - 1; b0 = 0.997 * b0 + 0.029 * w; b1 = 0.985 * b1 + 0.032 * w; b2 = 0.95 * b2 + 0.048 * w;
      d[i] = (b0 + b1 + b2 + w * 0.05) * 0.6;
    }
    return buf;
  }
  function startAmbient() {
    var ac = audio(); if (!ac || ambient) return;
    var master = ac.createGain(); master.gain.value = muted ? 0 : 0.5; master.connect(ac.destination);
    // wind: looping noise through a slowly wandering low-pass
    var wind = ac.createBufferSource(); wind.buffer = noiseBuffer(ac, 6); wind.loop = true;
    var lp = ac.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 420; lp.Q.value = 0.6;
    var wg = ac.createGain(); wg.gain.value = 0.16;
    var lfo = ac.createOscillator(), lfoG = ac.createGain(); lfo.frequency.value = 0.07; lfoG.gain.value = 220; lfo.connect(lfoG); lfoG.connect(lp.frequency);
    var lfo2 = ac.createOscillator(), lfo2G = ac.createGain(); lfo2.frequency.value = 0.11; lfo2G.gain.value = 0.06; lfo2.connect(lfo2G); lfo2G.connect(wg.gain);
    wind.connect(lp); lp.connect(wg); wg.connect(master); wind.start(); lfo.start(); lfo2.start();
    // leaves: high-passed noise that rustles in gusts
    var leaves = ac.createBufferSource(); leaves.buffer = noiseBuffer(ac, 4); leaves.loop = true;
    var hp = ac.createBiquadFilter(); hp.type = 'bandpass'; hp.frequency.value = 3200; hp.Q.value = 0.8;
    var lg = ac.createGain(); lg.gain.value = 0.0;
    leaves.connect(hp); hp.connect(lg); lg.connect(master); leaves.start();
    (function gust() {
      var t = ac.currentTime, len = 1.5 + Math.random() * 3;
      lg.gain.cancelScheduledValues(t); lg.gain.setValueAtTime(lg.gain.value, t);
      lg.gain.linearRampToValueAtTime(0.025 + Math.random() * 0.03, t + len * 0.4); lg.gain.linearRampToValueAtTime(0.004, t + len);
      setTimeout(gust, (len + 2 + Math.random() * 6) * 1000);
    })();
    // birds: little chirp phrases at random intervals, random pitch and stereo-ish position
    function chirp() {
      var t = ac.currentTime, base = 2200 + Math.random() * 1800, notes = 2 + Math.floor(Math.random() * 4);
      var pan = ac.createStereoPanner ? ac.createStereoPanner() : null; if (pan) { pan.pan.value = Math.random() * 1.6 - 0.8; pan.connect(master); }
      for (var i = 0; i < notes; i++) {
        var o = ac.createOscillator(), g = ac.createGain(), s = t + i * (0.09 + Math.random() * 0.08), d = 0.06 + Math.random() * 0.08;
        o.type = 'sine'; o.frequency.setValueAtTime(base * (0.9 + Math.random() * 0.3), s); o.frequency.exponentialRampToValueAtTime(base * (1.1 + Math.random() * 0.4), s + d);
        g.gain.setValueAtTime(0.0001, s); g.gain.exponentialRampToValueAtTime(0.05, s + 0.01); g.gain.exponentialRampToValueAtTime(0.0001, s + d);
        o.connect(g); g.connect(pan || master); o.start(s); o.stop(s + d + 0.02);
      }
      setTimeout(chirp, 2500 + Math.random() * 7000);
    }
    setTimeout(chirp, 1500);
    ambient = { master: master };
  }
  document.addEventListener('pointerdown', function () { startAmbient(); }, { once: true });
  $('mute').classList.toggle('off', muted); $('mute').textContent = muted ? '🔇' : '🔊';
  $('mute').onclick = function (e) {
    e.stopPropagation(); muted = !muted; save('mda_muted', muted);
    $('mute').classList.toggle('off', muted); $('mute').textContent = muted ? '🔇' : '🔊';
    if (ambient) ambient.master.gain.setTargetAtTime(muted ? 0 : 0.5, audio().currentTime, 0.2);
  };

  // ---------------------------------------------------------------- scenery: island sprites + the fox
  var ISLANDS = { isl1: { left: 74.251, top: 5.034, width: 13.174 }, isl2: { left: 66.467, top: 33.557, width: 7.784 }, isl3: { left: 21.557, top: 34.396, width: 6.587 } };
  Object.keys(ISLANDS).forEach(function (id) { var p = ISLANDS[id], el = $(id); el.style.left = p.left + '%'; el.style.top = p.top + '%'; el.style.width = p.width + '%'; el.style.aspectRatio = id === 'isl1' ? '220 / 230' : (id === 'isl2' ? '130 / 110' : '110 / 110'); });
  (function foxLoop() {
    var fox = $('fox'), back = Math.random() < 0.5;
    fox.className = 'fox run' + (back ? ' back' : '');
    setTimeout(function () { fox.className = 'fox'; }, 7200);
    setTimeout(foxLoop, 25000 + Math.random() * 45000);   // every 25–70 s
  })();

  // ---------------------------------------------------------------- eagles
  var flyers = $('flyers'), dir = 1, screechNext = false, eagleTimer = null;
  var eaglesOff = load('mda_eagles_off') === true;
  $('eagles-btn').classList.toggle('off', eaglesOff); $('eagles-btn').title = eaglesOff ? 'Eagles are off (tap to turn on)' : 'Eagles are on (tap to turn off)';
  $('eagles-btn').onclick = function (e) {
    e.stopPropagation(); eaglesOff = !eaglesOff; save('mda_eagles_off', eaglesOff);
    $('eagles-btn').classList.toggle('off', eaglesOff); $('eagles-btn').title = eaglesOff ? 'Eagles are off (tap to turn on)' : 'Eagles are on (tap to turn off)';
  };
  function fly(opts) {
    opts = opts || {};
    if (eaglesOff && !opts.force) return;
    var e = document.createElement('div'); e.className = 'flyer';
    var d = opts.dir || dir; dir = -d;                              // alternate every flight
    var y0 = opts.y0 != null ? opts.y0 : 8 + Math.random() * 60;   // vh
    var y1 = opts.y1 != null ? opts.y1 : 8 + Math.random() * 60;
    var dur = opts.dur || 3.2 + Math.random() * 1.2;
    var sc = opts.scale || 0.7 + Math.random() * 0.6;
    e.style.setProperty('--y0', y0 + 'vh'); e.style.setProperty('--y1', y1 + 'vh');
    e.style.setProperty('--dur', dur + 's'); e.style.setProperty('--sc', sc);
    e.classList.add(d > 0 ? 'ltr' : 'rtl');
    flyers.appendChild(e);
    if (opts.screech || screechNext) { screech(); screechNext = false; }
    setTimeout(function () { e.remove(); }, dur * 1000 + 200);
  }
  function flock() {   // 15 eagles from all directions, staggered within half a second
    for (var i = 0; i < 15; i++) {
      (function (i) {
        setTimeout(function () {
          fly({ force: true, dir: i % 2 ? -1 : 1, y0: 5 + Math.random() * 80, y1: 5 + Math.random() * 80, dur: 2.4 + Math.random() * 1.6, scale: 0.5 + Math.random() * 0.9, screech: i === 0 });
        }, i * 33);
      })(i);
    }
  }
  var UPLOAD_GAPS = [10, 25, 15, 45, 35];
  function eagleSchedule(mode) {
    clearTimeout(eagleTimer); eagleTimer = null;
    if (mode === 'gate') { var g = function () { fly(); eagleTimer = setTimeout(g, 6000); }; eagleTimer = setTimeout(g, 1200); }
    if (mode === 'upload') { var k = 0; var u = function () { fly(); eagleTimer = setTimeout(u, UPLOAD_GAPS[k++ % UPLOAD_GAPS.length] * 1000); }; eagleTimer = setTimeout(u, 1500); }
  }

  // ---------------------------------------------------------------- views
  var views = ['gate1', 'gate2', 'join', 'login', 'upload', 'mine', 'style', 'guide', 'admin'];
  function show(v) {
    views.forEach(function (x) { $('view-' + x).classList.toggle('hidden', x !== v); });
    var el = $('view-' + v); el.classList.remove('enter'); void el.offsetWidth; el.classList.add('enter');
    document.body.classList.toggle('gated', ['gate1', 'gate2', 'join', 'login'].indexOf(v) >= 0);
    document.body.classList.toggle('uploadview', v === 'upload');
    eagleSchedule(['gate1', 'gate2', 'join', 'login'].indexOf(v) >= 0 ? 'gate' : (v === 'upload' ? 'upload' : 'off'));
    document.querySelectorAll('.navbtn[data-view]').forEach(function (b) { b.classList.toggle('active', b.dataset.view === v); });
    if (v === 'mine') loadMine();
    if (v === 'style') loadStyle();
    if (v === 'admin') { loadStats(); loadAgents(); loadLog(); }
  }
  function applySession() {
    var on = !!(session && session.expires > Date.now());
    if (!on) session = null;
    $('nav').classList.toggle('hidden', !on);
    $('guide-cta').classList.toggle('hidden', !on);
    if (!on) return show('gate1');
    $('who').textContent = session.display_name;
    var admin = !!session.admin;
    $('hud-count').textContent = admin ? 'ADMIN MODE' : 'HEY ' + String(session.display_name).split(' ')[0].toUpperCase();
    if (!admin) api({ action: 'myCalls' }).then(function (j) {
      var week = Date.now() - 7 * 86400000;
      var n = j.calls.filter(function (c) { return new Date(c.uploaded_at).getTime() > week; }).length;
      $('hud-count').textContent = n + ' CALL' + (n === 1 ? '' : 'S') + ' UPLOADED THIS WEEK';
    }).catch(function () {});
    document.querySelectorAll('.admin-only').forEach(function (e) { e.classList.toggle('hidden', !admin); });
    document.querySelector('.navbtn[data-view="upload"]').classList.toggle('hidden', admin);
    document.querySelector('.navbtn[data-view="mine"]').classList.toggle('hidden', admin);
    document.querySelector('.navbtn[data-view="style"]').classList.toggle('hidden', admin);
    show(admin ? 'admin' : 'upload');
  }
  function signOut() { session = null; save('mda_session', null); applySession(); }
  document.querySelectorAll('.navbtn[data-view]').forEach(function (b) { b.onclick = function () { show(b.dataset.view); }; });
  // the HOW WE EDIT button glows only until its first ever click on this device
  if (load('mda_guide_seen')) $('guide-cta').classList.add('seen');
  $('guide-cta').addEventListener('click', function () { save('mda_guide_seen', 1); this.classList.add('seen'); });
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
        save('mda_session', session); $('login-pass').value = ''; screechNext = true; applySession();
        setTimeout(function () { if (screechNext) fly({ force: true }); }, 600);   // first flyover (with screech) always plays
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
    var tp = all ? Math.min(100, Math.round(got / all * 100)) : 0;
    $('total-fill').style.width = tp + '%'; $('total-pct').textContent = tp + '%';
    if (tp >= 25 && lastMilestone < 25) { blip(); lastMilestone = 25; }
    if (tp >= 50 && lastMilestone < 50) { blip(); lastMilestone = 50; }
    if (tp >= 75 && lastMilestone < 75) { blip(); lastMilestone = 75; }
  }
  var lastMilestone = 0;

  // ---------------------------------------------------------------- upload: run
  var uploading = false;
  window.addEventListener('beforeunload', function (e) { if (uploading) { e.preventDefault(); e.returnValue = ''; } });

  $('upload-btn').onclick = function () {
    if (uploading || !files.video || !files.audio) return;
    uploading = true; $('upload-btn').disabled = true; $('upload-err').textContent = '';
    progress.video = progress.audio = 0; lastMilestone = 0; $('total-fill').style.width = '0%'; $('total-pct').textContent = '0%'; $('total-wrap').classList.remove('hidden');
    var status = function (t) { $('upload-status').textContent = t.toUpperCase().replace(/\.\.\.|…/g, ''); $('upload-status').classList.toggle('loading', /ING/.test(t.toUpperCase())); };
    var callId, ids = {};
    status('Starting…');
    var mode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'clips'; save('mda_mode', mode);
    var zoom = mode === 'clips' && $('zoom').checked; save('mda_zoom', zoom ? 1 : 0);
    api({ action: 'startCall', note: $('note').value, mode: mode, zoom: zoom ? 1 : 0 })
      .then(function (j) { callId = j.callId; status('Uploading video…'); return uploadOne('video', files.video, callId); })
      .then(function (id) { ids.video = id; $('drop-video').classList.add('done'); status('Uploading call recording…'); return uploadOne('audio', files.audio, callId); })
      .then(function (id) { ids.audio = id; $('drop-audio').classList.add('done'); status('Finishing…');
        return api({ action: 'finishCall', callId: callId, videoFileId: ids.video, audioFileId: ids.audio }); })
      .then(function () {
        status('COMPLETE!'); $('total-fill').style.width = '100%'; $('total-pct').textContent = '100%'; uploading = false;
        document.body.classList.add('shake'); setTimeout(function () { document.body.classList.remove('shake'); }, 500);
        jingle(); flock();                       // 10 eagles from all directions + screech + level-complete jingle
        setTimeout(function () {
          $('upload-card').classList.add('hidden'); $('upload-done').classList.remove('hidden');
        }, 1600);
      })
      .catch(function (err) {
        uploading = false; status('');
        $('upload-err').textContent = err.message + ' — nothing was lost; press Upload to try again.';
        $('upload-btn').disabled = false;
      });
  };
  // zoom only means anything when we are cutting clips, so it hides in sync-only mode
  function syncZoomVisible() {
    var m = (document.querySelector('input[name="mode"]:checked') || {}).value || 'clips';
    $('zoom-switch').classList.toggle('hidden', m !== 'clips');
  }
  (function () {
    var m = load('mda_mode'); var r = m && document.querySelector('input[name="mode"][value="' + m + '"]'); if (r) r.checked = true;
    $('zoom').checked = !!load('mda_zoom');
    syncZoomVisible();
  })();
  $('zoom').onchange = function () { save('mda_zoom', this.checked ? 1 : 0); };
  document.querySelectorAll('input[name="mode"]').forEach(function (r) { r.onchange = function () {
    $('upload-btn').innerHTML = '<span class="arr">▶</span> ' + (r.value === 'sync' ? 'UPLOAD + SYNC ONLY' : 'UPLOAD BOTH FILES') + ' <span class="arr">◀</span>';
    syncZoomVisible();
  }; });
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
        return '<div class="item"><div class="t">' + esc(fmtDate(c.uploaded_at)) + (c.note ? ' · ' + esc(c.note) : '') + (c.mode === 'sync' ? ' · <span class="tag">SYNC ONLY</span>' : '') + '</div>' +
          '<span class="pill ' + esc(c.status) + '">' + esc(c.status) + (c.status === 'Ready' && c.clips ? ' · ' + c.clips + ' clips' : '') + '</span>' +
          (c.problem ? '<div class="m">' + esc(c.problem) + '</div>' : '') + '</div>';
      }).join('');
    }).catch(function (err) { el.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }


  // ---------------------------------------------------------------- my style
  var STYLE_PRESETS = [
    { name: 'MDA RED', font: 'Poppins', box: '#E01B2B', text: '#FFFFFF', hook_box: '#E01B2B', hook_text: '#FFFFFF', position: 'low' },
    { name: 'GOLD RUSH', font: 'Bebas Neue', box: '#FFCC1A', text: '#1A1000', hook_box: '#1B3A9E', hook_text: '#FFCC1A', position: 'low' },
    { name: 'MIDNIGHT', font: 'Montserrat', box: '#0B1F5C', text: '#FFFFFF', hook_box: '#4FD1FF', hook_text: '#0B1F5C', position: 'low' },
    { name: 'CLEAN WHITE', font: 'Oswald', box: '#FFFFFF', text: '#111111', hook_box: '#111111', hook_text: '#FFFFFF', position: 'low' },
    { name: 'COMIC', font: 'Bangers', box: '#FFE066', text: '#1A1000', hook_box: '#FF4D1C', hook_text: '#FFFFFF', position: 'mid' },
    { name: 'HYPE', font: 'Anton', box: '#000000', text: '#4CE0A5', hook_box: '#4CE0A5', hook_text: '#000000', position: 'low' }
  ];
  var styleFonts = ['Poppins', 'Montserrat', 'Bebas Neue', 'Anton', 'Oswald', 'Bangers', 'Luckiest Guy'];
  function readStyle() {
    return { font: $('st-font').value, box: $('st-box').value.toUpperCase(), text: $('st-text').value.toUpperCase(),
             hook_box: $('st-hook-box').value.toUpperCase(), hook_text: $('st-hook-text').value.toUpperCase(), position: $('st-pos').value };
  }
  function writeStyle(st) {
    $('st-font').value = st.font; $('st-box').value = st.box; $('st-text').value = st.text;
    $('st-hook-box').value = st.hook_box; $('st-hook-text').value = st.hook_text; $('st-pos').value = st.position; preview();
  }
  function preview() {
    var st = readStyle(), cap = $('pv-cap'), hook = $('pv-hook');
    cap.style.fontFamily = "'" + st.font + "', sans-serif"; cap.style.background = st.box; cap.style.color = st.text;
    hook.style.fontFamily = "'" + st.font + "', sans-serif"; hook.style.background = st.hook_box; hook.style.color = st.hook_text;
    cap.style.bottom = { low: '17%', mid: '27%', high: '40%' }[st.position];
  }
  function loadStyle() {
    if (!$('st-font').options.length) {
      styleFonts.forEach(function (f) { var o = document.createElement('option'); o.value = f; o.textContent = f; o.style.fontFamily = f; $('st-font').appendChild(o); });
      STYLE_PRESETS.forEach(function (p) {
        var b = document.createElement('button'); b.type = 'button'; b.className = 'preset'; b.textContent = p.name;
        b.style.background = p.box; b.style.color = p.text; b.style.fontFamily = "'" + p.font + "', sans-serif";
        b.onclick = function () { writeStyle(p); }; $('presets').appendChild(b);
      });
      ['st-font', 'st-box', 'st-text', 'st-hook-box', 'st-hook-text', 'st-pos'].forEach(function (id) { $(id).oninput = preview; $(id).onchange = preview; });
      $('st-save').onclick = function () {
        $('st-err').textContent = ''; $('st-status').textContent = 'SAVING…'; $('st-save').disabled = true;
        api({ action: 'setStyle', style: readStyle() }).then(function () { $('st-status').textContent = 'SAVED! Your next upload will use this style.'; blip(); })
          .catch(function (e) { $('st-status').textContent = ''; $('st-err').textContent = e.message; }).then(function () { $('st-save').disabled = false; });
      };
    }
    $('st-status').textContent = 'LOADING…';
    api({ action: 'getStyle' }).then(function (j) { writeStyle(j.style); $('st-status').textContent = ''; })
      .catch(function (e) { $('st-status').textContent = ''; $('st-err').textContent = e.message; });
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

  var statsPeriod = 'all';
  document.querySelectorAll('#stats-seg .btn').forEach(function (b) {
    b.onclick = function () {
      statsPeriod = b.dataset.period;
      document.querySelectorAll('#stats-seg .btn').forEach(function (x) { x.classList.toggle('active', x === b); });
      loadStats();
    };
  });
  function loadStats() {
    var tiles = $('stats-tiles'), list = $('stats-agents');
    tiles.innerHTML = '<div class="empty">Loading…</div>'; list.innerHTML = '';
    api({ action: 'adminStats', period: statsPeriod }).then(function (j) {
      var t = j.totals;
      var gb = t.video_mb >= 1024 ? (t.video_mb / 1024).toFixed(1) + ' GB' : Math.round(t.video_mb) + ' MB';
      tiles.innerHTML =
        tile(t.calls, 'CALLS UPLOADED') + tile(t.clips, 'CLIPS CREATED') + tile(t.ready, 'READY') +
        tile(t.working, 'WORKING') + tile(t.problems, 'PROBLEMS', t.problems > 0) + tile(gb, 'VIDEO UPLOADED');
      if (!j.agents.length) { list.innerHTML = '<div class="empty">No uploads ' + ({ day: 'today', week: 'in the last 7 days', month: 'in the last 30 days', all: 'yet' }[j.period] || '') + '.</div>'; return; }
      var max = Math.max.apply(null, j.agents.map(function (a) { return a.calls; }));
      list.innerHTML = j.agents.map(function (a) {
        return '<div class="item"><div style="flex:1"><div class="t">' + esc(a.display_name) + ' <span class="pill">' + esc(a.username) + '</span></div>' +
          '<div class="m">' + a.calls + ' call' + (a.calls === 1 ? '' : 's') + ' · ' + a.clips + ' clip' + (a.clips === 1 ? '' : 's') +
          (a.sync ? ' · ' + a.sync + ' sync-only' : '') + (a.working ? ' · ' + a.working + ' working' : '') +
          (a.problems ? ' · ' + a.problems + ' problem' + (a.problems === 1 ? '' : 's') : '') +
          ' · last upload ' + esc(fmtDate(a.last_upload) || '—') + '</div>' +
          '<div class="ubar"><i style="width:' + Math.max(4, Math.round(a.calls / max * 100)) + '%"></i></div></div></div>';
      }).join('');
    }).catch(function (err) { tiles.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }
  function tile(n, label, warn) { return '<div class="tile' + (warn ? ' warn' : '') + '"><div class="n">' + n + '</div><div class="l">' + label + '</div></div>'; }

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
          (r.problem ? ' · ' + esc(r.problem) : '') + '</div></div><span class="pill ' + esc(st) + '">' + esc(st) + (r.clips_delivered ? ' · ' + r.clips_delivered + ' clips' : '') + '</span>' +
          (r.status === 'problem' ? '<button class="btn small" data-retry="' + esc(r.call_id) + '">RETRY</button>' : '') + '</div>';
      }).join('');
      el.querySelectorAll('button[data-retry]').forEach(function (btn) {
        btn.onclick = function () {
          btn.disabled = true; btn.textContent = 'QUEUED…';
          api({ action: 'adminRequeue', callId: btn.dataset.retry }).then(function () { loadLog(); loadStats(); })
            .catch(function (e) { window.alert(e.message); btn.disabled = false; btn.textContent = 'RETRY'; });
        };
      });
    }).catch(function (err) { el.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }

  // ---------------------------------------------------------------- utils
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function fmtDate(d) { if (!d) return ''; var t = new Date(d); return isNaN(t) ? String(d) : t.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }

  applySession();
})();
