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
  var views = ['gate1', 'gate2', 'join', 'login', 'upload', 'mine', 'clips', 'style', 'guide', 'admin'];
  var lastCalls = null;                        // freshest myCalls rows, shared by the dup check and views
  function show(v) {
    views.forEach(function (x) { $('view-' + x).classList.toggle('hidden', x !== v); });
    var el = $('view-' + v); el.classList.remove('enter'); void el.offsetWidth; el.classList.add('enter');
    document.body.classList.toggle('gated', ['gate1', 'gate2', 'join', 'login'].indexOf(v) >= 0);
    document.body.classList.toggle('uploadview', v === 'upload');
    eagleSchedule(['gate1', 'gate2', 'join', 'login'].indexOf(v) >= 0 ? 'gate' : (v === 'upload' ? 'upload' : 'off'));
    document.querySelectorAll('.navbtn[data-view]').forEach(function (b) { b.classList.toggle('active', b.dataset.view === v); });
    if (v === 'mine') loadMine();
    if (v === 'clips') loadClips();
    if (v === 'style') loadStyle();
    if (v === 'admin') { loadStats(); loadAgents(); loadLog(); loadPosted(); }
    if (v === 'upload') loadTeamBoard();
  }
  function applySession() {
    var on = !!(session && session.expires > Date.now());
    if (!on) session = null;
    $('nav').classList.toggle('hidden', !on);
    $('guide-cta').classList.toggle('hidden', !on);
    $('suggest-fab').classList.toggle('hidden', !on);
    if (!on) return show('gate1');
    $('who').textContent = session.display_name;
    var admin = !!session.admin;
    $('hud-count').textContent = admin ? 'ADMIN MODE' : 'HEY ' + String(session.display_name).split(' ')[0].toUpperCase();
    if (!admin) api({ action: 'myCalls' }).then(function (j) {
      lastCalls = j.calls;
      var week = Date.now() - 7 * 86400000;
      var n = j.calls.filter(function (c) { return new Date(c.uploaded_at).getTime() > week; }).length;
      $('hud-count').textContent = n + ' CALL' + (n === 1 ? '' : 'S') + ' UPLOADED THIS WEEK';
    }).catch(function () {});
    document.querySelectorAll('.admin-only').forEach(function (e) { e.classList.toggle('hidden', !admin); });
    document.querySelector('.navbtn[data-view="upload"]').classList.toggle('hidden', admin);
    document.querySelector('.navbtn[data-view="mine"]').classList.toggle('hidden', admin);
    document.querySelector('.navbtn[data-view="style"]').classList.toggle('hidden', admin);
    document.querySelector('.navbtn[data-view="clips"]').classList.toggle('hidden', admin);
    if (admin) $('suggest-fab').classList.add('hidden');
    show(admin ? 'admin' : 'upload');
  }
  function signOut() { session = null; save('mda_session', null); applySession(); }
  document.querySelectorAll('.navbtn[data-view]').forEach(function (b) { b.onclick = function () { show(b.dataset.view); }; });
  // the HOW WE EDIT button glows only until its first ever click on this device
  if (load('mda_guide_seen')) $('guide-cta').classList.add('seen');
  // the SUPER EDITOR card is full holographic until the agent has used it once; after that it keeps its foil but stops flashing
  var holo = document.querySelector('.mode-opt.holo');
  if (holo && load('mda_super_seen')) holo.classList.add('seen');
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
    input.onchange = function () { var f = input.files[0]; if (repickDone(kind, f)) return; setFile(kind, f); };
    drop.ondragover = function (e) { e.preventDefault(); drop.classList.add('over'); };
    drop.ondragleave = function () { drop.classList.remove('over'); };
    drop.ondrop = function (e) { e.preventDefault(); drop.classList.remove('over'); if (!uploading && e.dataTransfer.files[0]) setFile(kind, e.dataTransfer.files[0]); };
  });
  function setFile(kind, f) {
    files[kind] = f || null;
    $('name-' + kind).textContent = f ? f.name + ' · ' + fmtMB(f.size) : '';
    $('drop-' + kind).classList.toggle('has', !!f);
    gateUpload();
  }
  var capStore = {};                           // call_id -> Instagram captions, for the copy button
  function legacyCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
  }
  function curMode() { return (document.querySelector('input[name="mode"]:checked') || {}).value || 'clips'; }
  function gateUpload() {
    $('upload-btn').disabled = !(files.video && (files.audio || curMode() === 'camera'));
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
    if (island.phase === 'upload') islandSet(tp, null);   // the island bar rides the upload
    if (tp >= 25 && lastMilestone < 25) { blip(); lastMilestone = 25; }
    if (tp >= 50 && lastMilestone < 50) { blip(); lastMilestone = 50; }
    if (tp >= 75 && lastMilestone < 75) { blip(); lastMilestone = 75; }
  }
  var lastMilestone = 0;

  // ---------------------------------------------------------------- upload: run
  var uploading = false;
  window.addEventListener('beforeunload', function (e) { if (uploading) { e.preventDefault(); e.returnValue = ''; } });

  $('upload-btn').onclick = function () {
    if (uploading || !files.video || (!files.audio && curMode() !== 'camera')) return;
    // same files as a recent upload? warn before burning another upload on it
    var vMB = Math.round(files.video.size / 1048576);
    var aMB = files.audio ? Math.round(files.audio.size / 1048576) : 0;
    var week = Date.now() - 7 * 86400000;
    var prior = (lastCalls || []).filter(function (c) {
      return new Date(c.uploaded_at).getTime() > week && vMB > 0 && Number(c.video_MB) === vMB &&
             (curMode() === 'camera' ? Number(c.audio_MB) === 0 : Number(c.audio_MB) === aMB);
    })[0];
    if (prior) {
      $('dup-text').innerHTML = 'These look like the <b>same files</b> you uploaded ' + esc(fmtDate(prior.uploaded_at)) +
        (prior.note ? ' ("' + esc(prior.note) + '")' : '') + ' — that call is marked <b>' + esc(prior.status) + '</b>. ' +
        (prior.status === 'Problem'
          ? 'Re-uploading the same files gives the same result — if something looks wrong, tell Mason instead.'
          : prior.status === 'Ready' ? 'Your clips for it are already in your Drive folder.'
          : 'It is still being worked on — no need to send it again.');
      $('dup-modal').classList.remove('hidden');
      return;
    }
    beginUpload();
  };
  $('dup-go').onclick = function () { $('dup-modal').classList.add('hidden'); beginUpload(); };
  $('dup-no').onclick = function () { $('dup-modal').classList.add('hidden'); };
  function beginUpload() {
    if (uploading || !files.video || (!files.audio && curMode() !== 'camera')) return;
    uploading = true; $('upload-btn').disabled = true; $('upload-err').textContent = '';
    progress.video = progress.audio = 0; lastMilestone = 0; $('total-fill').style.width = '0%'; $('total-pct').textContent = '0%'; $('total-wrap').classList.remove('hidden');
    var status = function (t) { $('upload-status').textContent = t.toUpperCase().replace(/\.\.\.|…/g, ''); $('upload-status').classList.toggle('loading', /ING/.test(t.toUpperCase()));
      if (island.phase === 'upload') $('isl-label').textContent = t.toUpperCase(); };
    var callId, ids = {};
    status('Starting…');
    var mode = curMode(); save('mda_mode', mode);
    if (mode === 'clips' && holo) { save('mda_super_seen', 1); holo.classList.add('seen'); }
    var zoom = (mode === 'clips' || mode === 'camera') && $('zoom').checked; save('mda_zoom', zoom ? 1 : 0);
    var nm = (document.querySelector('input[name="nm"]:checked') || {}).value || 'bleep'; save('mda_namemode', nm);
    var cap = (document.querySelector('input[name="cap"]:checked') || {}).value || 'on'; save('mda_captions', cap);
    islandUploading();                         // fly straight to the island; its bar carries the upload
    api({ action: 'startCall', note: $('note').value, mode: mode, zoom: zoom ? 1 : 0, name_mode: nm, captions: cap })
      .then(function (j) {
        callId = j.callId;
        if (mode === 'camera' || !files.audio) return '';   // talking to the audience: there is no call recording
        // the call recording goes FIRST: it is the small file, and whichever file waits its turn is the one
        // a phone can release mid-upload. Seconds of waiting instead of the length of a 900 MB video.
        status('Uploading call recording…'); return uploadOne('audio', files.audio, callId);
      })
      .then(function (id) { ids.audio = id; if (id) $('drop-audio').classList.add('done');
        status('Uploading video…'); return uploadOne('video', files.video, callId); })
      .then(function (id) { ids.video = id; $('drop-video').classList.add('done'); return ids.audio; })
      .then(function (id) { ids.audio = id; if (id) $('drop-audio').classList.add('done'); status('Finishing…');
        return api({ action: 'finishCall', callId: callId, videoFileId: ids.video, audioFileId: ids.audio || '' }); })
      .then(function () {
        status('COMPLETE!'); $('total-fill').style.width = '100%'; $('total-pct').textContent = '100%'; uploading = false;
        document.body.classList.add('shake'); setTimeout(function () { document.body.classList.remove('shake'); }, 500);
        jingle(); flock();                       // 10 eagles from all directions + screech + level-complete jingle
        islandSet(100, 'FILES LANDED — THE CREW TAKES OVER!');
        var isl = $('editor-island');            // the eagles pass, the crew dances, then runs into the theater
        setTimeout(function () { isl.classList.add('isl-party'); }, 1500);
        setTimeout(function () { isl.classList.remove('isl-party'); isl.classList.add('isl-runin'); }, 4000);
        setTimeout(function () { isl.classList.remove('isl-runin'); islandEditing(callId); }, 5900);
      })
      .catch(function (err) {
        uploading = false; status('');
        islandLeave();
        $('upload-err').textContent = err.message + ' — nothing was lost; press Upload to try again.';
        $('upload-btn').disabled = false;
      });
  };
  // each mode shows only the choices that mean something in it
  var ZOOM_COPY_CALL = document.querySelector('.zoom-copy') ? document.querySelector('.zoom-copy').innerHTML : '';
  var ZOOM_COPY_CAM = 'The frame swaps to a subtle punch-in at every cut, so your jump cuts read as a choice — not a glitch.';
  function syncZoomVisible() {
    var m = curMode();
    $('zoom-switch').classList.toggle('hidden', m === 'sync');
    $('nm-block').classList.toggle('hidden', m !== 'clips');
    $('cap-block').classList.toggle('hidden', m !== 'camera');
    $('drop-audio').classList.toggle('hidden', m === 'camera');
    var zc = document.querySelector('.zoom-copy');
    if (zc && ZOOM_COPY_CALL) zc.innerHTML = m === 'camera' ? ZOOM_COPY_CAM : ZOOM_COPY_CALL;
    if (m === 'camera' && files.audio) {       // a call recording picked earlier has no place here
      setFile('audio', null); setBar('audio', 0, 0); $('drop-audio').classList.remove('done'); $('file-audio').value = '';
    }
    gateUpload();
  }
  (function () {
    var m = load('mda_mode'); var r = m && document.querySelector('input[name="mode"][value="' + m + '"]'); if (r) r.checked = true;
    $('zoom').checked = !!load('mda_zoom');
    var n = load('mda_namemode'); var nr = n && document.querySelector('input[name="nm"][value="' + n + '"]'); if (nr) nr.checked = true;
    var cp = load('mda_captions'); var cr = cp && document.querySelector('input[name="cap"][value="' + cp + '"]'); if (cr) cr.checked = true;
    $('upload-btn').innerHTML = '<span class="arr">▶</span> ' + (curMode() === 'sync' ? 'UPLOAD + SYNC ONLY'
      : curMode() === 'camera' ? 'UPLOAD MY VIDEO' : 'UPLOAD BOTH FILES') + ' <span class="arr">◀</span>';
    syncZoomVisible();
  })();
  document.querySelectorAll('input[name="nm"]').forEach(function (r) { r.onchange = function () { save('mda_namemode', r.value); }; });
  document.querySelectorAll('input[name="cap"]').forEach(function (r) { r.onchange = function () { save('mda_captions', r.value); }; });
  $('zoom').onchange = function () { save('mda_zoom', this.checked ? 1 : 0); };
  document.querySelectorAll('input[name="mode"]').forEach(function (r) { r.onchange = function () {
    $('upload-btn').innerHTML = '<span class="arr">▶</span> ' + (r.value === 'sync' ? 'UPLOAD + SYNC ONLY'
      : r.value === 'camera' ? 'UPLOAD MY VIDEO' : 'UPLOAD BOTH FILES') + ' <span class="arr">◀</span>';
    syncZoomVisible();
  }; });
  $('upload-again').onclick = function () {
    ['video', 'audio'].forEach(function (k) { setFile(k, null); setBar(k, 0, 0); $('drop-' + k).classList.remove('done'); $('file-' + k).value = ''; });
    $('note').value = ''; $('total-wrap').classList.add('hidden'); $('upload-status').textContent = '';
    islandLeave();
  };

  // ---------- EDITOR ISLAND: fly there on upload, watch the crew cut the clips ----------
  var island = { callId: null, timer: null, pct: 0, phase: null };
  function islandSet(p, label) {
    $('isl-fill').style.width = p + '%';
    $('isl-pct').textContent = p + '%';
    $('isl-mon-pct').textContent = p + '%';
    if (label) $('isl-label').textContent = label;
  }
  function usherInvite() {                     // one invite per visit: 15s after the island appears, up for 15s, then he just stands there
    if (island.usherSpoke || island.usherTimer) return;
    island.usherTimer = setTimeout(function () {
      island.usherSpoke = true;
      $('isl-usher-say').classList.remove('hidden'); blip();
      island.usherTimer = setTimeout(function () { $('isl-usher-say').classList.add('hidden'); island.usherTimer = null; }, 15000);
    }, 15000);
  }
  function islandUploading() {                 // phase 1: the flight in, bar shows the upload itself
    island.phase = 'upload'; island.pct = 0;
    $('upload-card').classList.add('hidden');
    document.body.classList.add('on-island');
    $('editor-island').classList.remove('hidden', 'isl-done', 'isl-problem', 'isl-party', 'isl-runin');
    $('editor-island').classList.add('flying');
    setTimeout(function () { $('editor-island').classList.remove('flying'); }, 2200);
    $('isl-bubble').classList.add('hidden'); $('isl-folder').classList.add('hidden');
    $('isl-err').textContent = '';
    islandSet(0, 'FLYING YOUR FILES TO EDITOR ISLAND…');
    $('isl-note').textContent = 'Keep this page open until the upload bar hits 100% — after that the crew takes it and you can go.';
    usherInvite();                             // the countdown starts the moment they land, so they can step in during the upload
  }
  function islandEditing(callId) {             // phase 2: the crew works, bar shows real edit stages
    island.callId = callId; island.pct = 2; island.phase = 'edit';
    document.body.classList.add('on-island');
    $('editor-island').classList.remove('hidden', 'isl-done', 'isl-problem', 'isl-party', 'isl-runin');
    $('isl-bubble').classList.add('hidden'); $('isl-folder').classList.add('hidden');
    $('isl-err').textContent = '';
    islandSet(2, 'CHECKING IN AT THE EDIT BAY…');
    $('isl-note').textContent = 'The crew is on it. We already emailed you a receipt — you can close this page anytime, and another email lands the moment your videos are ready.';
    usherInvite();                             // covers landing straight in the edit phase (page reopened mid-edit)
    islandPoll();
    island.timer = setInterval(islandPoll, 20000);
  }
  window.MDA_ISLAND = { up: islandUploading, edit: islandEditing };   // lets automated checks drive the island
  function islandReset() {
    if (island.timer) clearInterval(island.timer);
    clearTimeout(island.usherTimer); island.usherTimer = null;
    $('isl-usher-say').classList.add('hidden');
    island.timer = null; island.callId = null; island.pct = 0; island.phase = null;
  }
  function islandLeave() {                     // back to the upload form: the island scene stands down
    islandReset();
    document.body.classList.remove('on-island');
    $('editor-island').classList.add('hidden'); $('upload-card').classList.remove('hidden');
  }
  function islandPoll() {
    if (!island.callId) return;
    api({ action: 'myCalls' }).then(function (j) {
      lastCalls = j.calls;
      var c = (j.calls || []).filter(function (x) { return x.call_id === island.callId; })[0];
      if (!c) return;
      if (c.status === 'Ready') return islandDone(c);
      if (c.status === 'Problem') return islandProblem(c);
      var pr = String(c.progress || '').split('|');
      var p = Math.min(99, parseInt(pr[0], 10) || 0);
      if (p <= island.pct) p = Math.min(island.pct + 1, 99);   // never looks frozen between stages
      island.pct = p;
      var label = (pr[1] || (c.status === 'Editing' ? 'The crew is editing…' : 'Waiting for an open edit bay…'));
      islandSet(p, label.toUpperCase());
    }).catch(function () {});
  }
  function islandDone(c) {
    islandReset();
    islandSet(100, 'ALL DONE — CHECK YOUR DRIVE!');
    $('editor-island').classList.add('isl-done');
    $('isl-bubble').classList.remove('hidden');
    $('isl-note').textContent = 'Delivered! Your clips are waiting in your Drive, and the email is on its way to your inbox.';
    if (c.folder_url) { $('isl-folder').href = c.folder_url; $('isl-folder').classList.remove('hidden'); }
    var conf = $('isl-confetti'); conf.innerHTML = '';
    for (var i = 0; i < 26; i++) {
      var s = document.createElement('i');
      s.style.left = (3 + Math.random() * 94) + '%';
      s.style.animationDelay = (Math.random() * 1.6) + 's';
      s.style.background = ['#fbc716', '#0553d0', '#b9d6f8', '#e8313a', '#15a98a'][i % 5];
      conf.appendChild(s);
    }
    jingle();
  }
  function islandProblem(c) {
    islandReset();
    islandSet(island.pct, 'THE CREW HIT A SNAG.');
    $('editor-island').classList.add('isl-problem');
    $('isl-err').textContent = (c.problem || 'Something went wrong with this call.') + ' — check your email, then press Upload Another Call to retry.';
  }

  // Chunked, resumable upload straight to Google Drive (session URL comes from the doorman).
  // A phone can release a picked file while a long upload runs - the handle survives but its bytes
  // no longer read, and fetch() reports that as a bare "Failed to fetch". Probe before blaming the network.
  function readable(file) {
    try {
      return file.slice(0, 1).arrayBuffer().then(function () { return true; }, function () { return false; });
    } catch (e) { return Promise.resolve(false); }
  }
  var repick = null;                           // { kind, resolve } while we wait for the agent to re-pick
  function askRepick(kind) {
    return new Promise(function (resolve) {
      repick = { kind: kind, resolve: resolve };
      var what = kind === 'audio' ? 'call recording' : 'video';
      $('upload-status').textContent = 'YOUR PHONE LET GO OF THE ' + what.toUpperCase();
      $('upload-status').classList.remove('loading');
      $('upload-err').innerHTML = 'Your phone released the ' + what + ' while the other file was uploading — ' +
        'nothing you have already sent is lost. <b>Pick the same ' + what + ' again</b> and it carries on from here.';
      $('repick-btn').textContent = '\u25B6 PICK THE ' + what.toUpperCase() + ' AGAIN';
      $('repick-btn').classList.remove('hidden');
      $('repick-btn').onclick = function () { $('file-' + kind).click(); };
    });
  }
  function repickDone(kind, f) {               // called from the file inputs when a recovery pick lands
    if (!repick || repick.kind !== kind || !f) return false;
    var r = repick; repick = null;
    $('repick-btn').classList.add('hidden'); $('upload-err').textContent = '';
    files[kind] = f;
    r.resolve(f);
    return true;
  }

  function uploadOne(kind, file, callId) {
    var key = 'mda_up_' + callId + '_' + kind;
    var saved = load(key);
    var start = saved && saved.name === file.name && saved.size === file.size ? Promise.resolve(saved.url) :
      api({ action: 'startUpload', token: session.token, callId: callId, kind: kind, filename: file.name, size: file.size,
            mimeType: file.type || 'application/octet-stream', origin: location.origin })
        .then(function (j) { save(key, { url: j.uploadUrl, name: file.name, size: file.size }); return j.uploadUrl; });

    return start.then(function (url) {
      var run = function (f) {
        return readable(f).then(function (ok) {
          if (ok) return queryOffset(url, f.size).then(function (offset) { return sendFrom(url, f, offset, kind, 0); });
          return askRepick(kind).then(run);    // the handle died: get it back, resume the same session
        });
      };
      return run(file);
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
        if (/expired|released/.test(err.message) || retries >= 8) throw err;
        return readable(file).then(function (ok) {
          if (!ok) throw new Error('released');   // not the network - the phone dropped the file
          return retryLater(url, file, kind, retries);
        });
      })
      .catch(function (err) {
        if (/expired|released/.test(err.message) || retries >= 8) throw err;
        var wait = Math.min(30000, 1000 * Math.pow(2, retries));
        $('upload-status').textContent = 'Connection hiccup — resuming in ' + Math.round(wait / 1000) + 's…';
        return new Promise(function (res) { setTimeout(res, wait); })
          .then(function () { return queryOffset(url, size); })
          .then(function (off) { return sendFrom(url, file, off, kind, retries + 1); });
      });
  }
  function retryLater(url, file, kind, retries) {
    var wait = Math.min(30000, 1000 * Math.pow(2, retries));
    $('upload-status').textContent = 'Connection hiccup \u2014 resuming in ' + Math.round(wait / 1000) + 's\u2026';
    return new Promise(function (res) { setTimeout(res, wait); })
      .then(function () { return queryOffset(url, file.size); })
      .then(function (off) { return sendFrom(url, file, off, kind, retries + 1); });
  }
  function finalId(url, size) {
    return fetch(url, { method: 'PUT', headers: { 'Content-Range': 'bytes */' + size } })
      .then(function (r) { return r.json(); }).then(function (j) { if (!j.id) throw new Error('could not confirm upload'); return j.id; });
  }

  // ---------------------------------------------------------------- my uploads
  function loadMine() {
    var el = $('mine-list'); el.innerHTML = '<div class="empty">Loading…</div>';
    api({ action: 'myCalls' }).then(function (j) {
      lastCalls = j.calls;
      $('my-folder').href = j.folder_url;
      if (!j.calls.length) { el.innerHTML = '<div class="empty">No uploads yet.</div>'; return; }
      el.innerHTML = j.calls.map(function (c) {
        var title = c.note ? esc(c.note) : 'Untitled call';
        var names = [];
        try { names = JSON.parse(c.clip_names || '[]') || []; } catch (e) { names = []; }
        var canEdit = c.status === 'Ready' && c.mode !== 'sync' && c.mode !== 'camera' && !c.raw_gone;
        var body = '';
        if (c.mode === 'sync') body += '<div class="m">Synced full video — no clips were cut for this one.</div>';
        if (c.mode === 'camera') body += '<div class="m">Talking-to-camera video — retakes and dead air cut, your last take kept.</div>';
        else if (names.length) body += '<div class="m">' + names.length + ' clip' + (names.length === 1 ? '' : 's') + ' delivered:</div><ul class="clips">' + names.map(function (n, idx) {
          return '<li>🎬 ' + esc(n) +
            (canEdit ? ' <button class="btn tiny fixw" type="button" data-cid="' + esc(c.call_id) + '" data-clip="' + (idx + 1) + '">✏ FIX A WORD</button>' : '') + '</li>';
        }).join('') + '</ul>';
        else if (c.status === 'Ready' && c.clips) body += '<div class="m">' + esc(c.clips) + ' clips delivered. (Clip names show here for uploads from now on.)</div>';
        else body += '<div class="m">Nothing delivered yet — this fills in when the edit finishes.</div>';
        if (canEdit) body += '<div class="more-row"><input class="mini" type="number" min="1" max="599" placeholder="minute (optional)" data-min="' + esc(c.call_id) + '">' +
          '<button class="btn small morec" type="button" data-cid="' + esc(c.call_id) + '">🎬 CUT MORE CLIPS</button></div>';
        if (c.folder_url) body += '<a class="btn small" href="' + esc(c.folder_url) + '" target="_blank" rel="noopener">OPEN THIS CALL\'S FOLDER ↗</a>';
        return '<div class="item call">' +
          '<button class="call-head" type="button">' +
            '<span class="arrow">▶</span>' +
            '<span class="call-title">' + title + (c.mode === 'sync' ? ' <span class="tag">SYNC ONLY</span>'
              : c.mode === 'camera' ? ' <span class="tag">TO CAMERA</span>'
              : c.mode === 'section' ? ' <span class="tag">ONE SECTION</span>' : '') + '<small>' + esc(fmtDate(c.uploaded_at)) +
            (c.status === 'Editing' && c.progress ? ' · ⚙ ' + esc(String(c.progress).split('|')[1] || 'editing') + ' — ' + esc(String(c.progress).split('|')[0] || '0') + '%' : '') + '</small></span>' +
            '<span class="pill ' + esc(c.status) + '">' + (c.job && c.status !== 'Ready' ? 'Re-editing' : esc(c.status)) + (c.status === 'Ready' && c.clips ? ' · ' + c.clips + ' clips' : '') + '</span>' +
          '</button>' +
          (c.problem ? '<div class="m prob">' + esc(c.problem) + '</div>' : '') +
          '<div class="call-body">' + body + '</div>' +
          '</div>';
      }).join('');
      el.querySelectorAll('.call-head').forEach(function (h) {
        h.addEventListener('click', function () { h.closest('.call').classList.toggle('open'); });
      });
      el.querySelectorAll('.fixw').forEach(function (b) {
        b.onclick = function (e) { e.stopPropagation(); openFix(b.dataset.cid, Number(b.dataset.clip)); };
      });
      el.querySelectorAll('.morec').forEach(function (b) {
        b.onclick = function (e) {
          e.stopPropagation();
          var inp = el.querySelector('[data-min="' + b.dataset.cid + '"]');
          var minute = inp && inp.value ? Number(inp.value) : 0;
          b.disabled = true; b.textContent = 'ASKING THE CREW…';
          api({ action: 'moreClips', callId: b.dataset.cid, minute: minute })
            .then(function () { blip(); loadMine(); })
            .catch(function (err) { b.disabled = false; b.textContent = '🎬 CUT MORE CLIPS'; window.alert(err.message); });
        };
      });
    }).catch(function (err) { el.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }

  // ---------------------------------------------------------------- fix a word
  var fixing = null;
  function openFix(cid, clip) {
    var c = (lastCalls || []).filter(function (x) { return x.call_id === cid; })[0];
    var caps = [];
    try { caps = JSON.parse((c && c.clip_captions) || '[]') || []; } catch (e) { caps = []; }
    fixing = { cid: cid, clip: clip };
    $('fix-cap').textContent = caps[clip - 1] || '(The caption text for this clip is not on file — type the word exactly as it appears in the video.)';
    $('fix-from').value = ''; $('fix-to').value = ''; $('fix-err').textContent = '';
    $('fix-modal').classList.remove('hidden');
    setTimeout(function () { $('fix-from').focus(); }, 150);
  }
  $('fix-no').onclick = function () { $('fix-modal').classList.add('hidden'); };
  $('fix-send').onclick = function () {
    if (!fixing) return;
    var frm = $('fix-from').value.trim(), to = $('fix-to').value.trim();
    if (!frm || !to) { $('fix-err').textContent = 'Fill in both boxes.'; return; }
    $('fix-send').disabled = true; $('fix-err').textContent = '';
    api({ action: 'fixCaption', callId: fixing.cid, clip: fixing.clip, from: frm, to: to })
      .then(function () {
        $('fix-send').disabled = false; $('fix-modal').classList.add('hidden'); blip(); loadMine();
      })
      .catch(function (err) { $('fix-send').disabled = false; $('fix-err').textContent = err.message; });
  };

  // ---------------------------------------------------------------- my clips gallery
  function loadClips() {
    var el = $('clip-grid'); el.innerHTML = '<div class="empty">Loading…</div>';
    api({ action: 'myCalls' }).then(function (j) {
      lastCalls = j.calls;
      $('clips-folder').href = j.folder_url;
      var calls = (j.calls || []).slice().sort(function (a, b) { return new Date(b.uploaded_at) - new Date(a.uploaded_at); });
      var boxes = [];
      calls.forEach(function (c) {
        var ids = [], names = [], thumbs = [], igs = [], posted = [];
        try { ids = JSON.parse(c.clip_ids || '[]') || []; } catch (e) { ids = []; }
        try { names = JSON.parse(c.clip_names || '[]') || []; } catch (e) { names = []; }
        try { thumbs = JSON.parse(c.clip_thumbs || '[]') || []; } catch (e) { thumbs = []; }
        try { igs = JSON.parse(c.clip_igcaps || '[]') || []; } catch (e) { igs = []; }
        try { posted = JSON.parse(c.clip_posted || '[]') || []; } catch (e) { posted = []; }
        if (!ids.length) return;
        capStore[c.call_id] = igs;
        var canEdit = c.status === 'Ready' && c.mode !== 'sync' && c.mode !== 'camera' && !c.raw_gone;
        var tiles = ids.map(function (id, i) {
          var nm = String(names[i] || 'Clip ' + (i + 1)).split(' - 20')[0];
          return '<div class="cliptile" data-id="' + esc(id) + '">' +
            '<div class="thumbwrap"' + (thumbs[i] ? ' data-thumb="' + esc(thumbs[i]) + '"' : '') + '><img loading="lazy" alt="" src="https://drive.google.com/thumbnail?id=' + esc(id) + '&sz=w400" onerror="this.parentNode.classList.add(\'noimg\')"><span class="play">▶</span></div>' +
            '<b>' + esc(nm) + '</b>' +
            '<div class="tile-btns"><a class="btn tiny dlbtn" href="https://drive.google.com/uc?export=download&id=' + esc(id) + '" target="_blank" rel="noopener">⬇ DOWNLOAD</a>' +
            (igs[i] ? '<button class="btn tiny capbtn" type="button" data-cid="' + esc(c.call_id) + '" data-clip="' + (i + 1) + '">📋 CAPTION</button>' : '') +
            '<button class="btn tiny postbtn' + (posted[i] ? ' on' : '') + '" type="button" data-cid="' + esc(c.call_id) + '" data-clip="' + (i + 1) + '">' + (posted[i] ? '✔ POSTED' : '🚀 MARK POSTED') + '</button>' +
            (canEdit ? '<button class="btn tiny fixw" type="button" data-cid="' + esc(c.call_id) + '" data-clip="' + (i + 1) + '">✏ FIX A WORD</button>' : '') + '</div>' +
            '</div>';
        }).join('');
        var title = c.note ? esc(c.note) : esc(fmtDateET(c.uploaded_at));
        boxes.push('<div class="upload-box" data-call="' + esc(c.call_id) + '">' +
          '<div class="ub-head"><b class="ub-title">🎬 ' + title + '</b>' +
          '<small>' + (c.note ? esc(fmtDateET(c.uploaded_at)) + ' · ' : '') + ids.length + ' clip' + (ids.length === 1 ? '' : 's') +
          ' · ' + ids.filter(function (_x, k) { return posted[k]; }).length + '/' + ids.length + ' posted</small>' +
          (c.job ? '<span class="pill">Re-editing</span>' : '') + '</div>' +
          '<div class="ub-grid">' + tiles + '</div>' +
          (canEdit ? '<div class="more-row"><input class="mini" type="number" min="1" max="599" placeholder="minute (optional)" data-min="g' + esc(c.call_id) + '">' +
            '<button class="btn small morec" type="button" data-cid="' + esc(c.call_id) + '">🎬 CUT MORE CLIPS</button></div>' : '') +
          '</div>');
      });
      el.innerHTML = boxes.length ? boxes.join('') :
        '<div class="empty">Your clips collect here from now on — anything cut before today lives in <a href="' + esc(j.folder_url) + '" target="_blank" rel="noopener">your Drive folder</a>.</div>';
      el.querySelectorAll('.cliptile').forEach(function (t) {
        t.addEventListener('click', function (e) {
          if (e.target.closest('.dlbtn') || e.target.closest('.fixw')) return;
          window.open('https://drive.google.com/file/d/' + t.dataset.id + '/view', '_blank', 'noopener');
        });
      });
      el.querySelectorAll('.fixw').forEach(function (b) {
        b.onclick = function (e) { e.stopPropagation(); openFix(b.dataset.cid, Number(b.dataset.clip)); };
      });
      el.querySelectorAll('.morec').forEach(function (b) {
        b.onclick = function (e) {
          e.stopPropagation();
          var inp = el.querySelector('[data-min="g' + b.dataset.cid + '"]');
          var minute = inp && inp.value ? Number(inp.value) : 0;
          b.disabled = true; b.textContent = 'ASKING THE CREW…';
          api({ action: 'moreClips', callId: b.dataset.cid, minute: minute })
            .then(function () { blip(); loadClips(); })
            .catch(function (err) { b.disabled = false; b.textContent = '🎬 CUT MORE CLIPS'; window.alert(err.message); });
        };
      });
      el.querySelectorAll('.capbtn').forEach(function (b) {
        b.onclick = function (e) {
          e.stopPropagation();
          var cap = (capStore[b.dataset.cid] || [])[Number(b.dataset.clip) - 1] || '';
          if (!cap) return;
          var done = function () { var t = b.textContent; b.textContent = '✔ COPIED!'; setTimeout(function () { b.textContent = t; }, 1600); blip(); };
          if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(cap).then(done).catch(function () { legacyCopy(cap); done(); });
          else { legacyCopy(cap); done(); }
        };
      });
      el.querySelectorAll('.postbtn').forEach(function (b) {
        b.onclick = function (e) {
          e.stopPropagation();
          var on = !b.classList.contains('on');
          b.classList.toggle('on', on); b.textContent = on ? '✔ POSTED' : '🚀 MARK POSTED'; b.disabled = true;
          api({ action: 'markPosted', callId: b.dataset.cid, clip: Number(b.dataset.clip), on: on ? 1 : 0 })
            .then(function () { b.disabled = false; blip(); loadClips(); })
            .catch(function (err) { b.disabled = false; b.classList.toggle('on', !on); b.textContent = !on ? '✔ POSTED' : '🚀 MARK POSTED'; window.alert(err.message); });
        };
      });
      el.querySelectorAll('.upload-box').forEach(function (box) {
        if (!box.querySelector('[data-thumb]')) return;
        api({ action: 'clipThumbs', callId: box.dataset.call }).then(function (r) {
          var map = (r && r.thumbs) || {};
          box.querySelectorAll('.thumbwrap[data-thumb]').forEach(function (w) {
            var d = map[w.dataset.thumb];
            if (d && /^data:image\//.test(d)) { w.querySelector('img').src = d; w.classList.remove('noimg'); }
          });
        }).catch(function () {});
      });
    }).catch(function (err) { el.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }

  // ---------------------------------------------------------------- suggestion box
  $('suggest-fab').onclick = function () {
    $('sug-text').value = ''; $('sug-err').textContent = ''; $('sug-ok').textContent = '';
    $('sug-send').disabled = false;
    $('sug-modal').classList.remove('hidden');
    setTimeout(function () { $('sug-text').focus(); }, 150);
  };
  $('sug-no').onclick = function () { $('sug-modal').classList.add('hidden'); };
  $('sug-send').onclick = function () {
    var text = $('sug-text').value.trim();
    if (text.length < 3) { $('sug-err').textContent = 'Write a little more first.'; return; }
    $('sug-send').disabled = true; $('sug-err').textContent = '';
    api({ action: 'suggest', text: text })
      .then(function () {
        $('sug-ok').textContent = 'SENT! Mason reads every one.'; blip();
        setTimeout(function () { $('sug-modal').classList.add('hidden'); }, 1400);
      })
      .catch(function (err) { $('sug-send').disabled = false; $('sug-err').textContent = err.message; });
  };
  document.querySelectorAll('.modal').forEach(function (m) {
    m.addEventListener('click', function (e) { if (e.target === m) m.classList.add('hidden'); });
  });


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
             hook_box: $('st-hook-box').value.toUpperCase(), hook_text: $('st-hook-text').value.toUpperCase(), position: $('st-pos').value,
             hook_position: $('st-hook-pos').value, corners: $('st-corners').value, cap_look: $('st-look').value };
  }
  function writeStyle(st) {
    $('st-font').value = st.font; $('st-box').value = st.box; $('st-text').value = st.text;
    $('st-hook-box').value = st.hook_box; $('st-hook-text').value = st.hook_text; $('st-pos').value = st.position;
    $('st-hook-pos').value = ['mid', 'low'].indexOf(st.hook_position) >= 0 ? st.hook_position : 'high';
    $('st-corners').value = st.corners === 'rounded' ? 'rounded' : 'square';
    $('st-look').value = st.cap_look === 'insta' ? 'insta' : 'classic'; preview();
  }
  function preview() {
    var st = readStyle(), cap = $('pv-cap'), hook = $('pv-hook');
    cap.style.fontFamily = "'" + st.font + "', sans-serif"; cap.style.background = st.box; cap.style.color = st.text;
    hook.style.fontFamily = "'" + st.font + "', sans-serif"; hook.style.background = st.hook_box; hook.style.color = st.hook_text;
    cap.style.borderRadius = hook.style.borderRadius = st.corners === 'rounded' ? '14px' : '0';
    if (st.cap_look === 'insta') {                     // IG look: bare italic line, pill on the spoken word
      cap.style.background = 'transparent'; cap.style.color = '#FFFFFF';
      cap.style.fontStyle = 'italic'; cap.style.textShadow = '2px 3px 7px rgba(0,0,0,.9)';
      cap.innerHTML = 'so what made you <span style="background:' + st.box + ';border-radius:10px;padding:1px 8px;text-shadow:none">look</span> into coverage';
    } else {
      cap.style.fontStyle = 'normal'; cap.style.textShadow = 'none';
      cap.style.background = st.box; cap.style.color = st.text;
      cap.textContent = 'SO WHAT MADE YOU LOOK INTO COVERAGE TODAY';
    }
    cap.style.bottom = { low: '17%', mid: '27%', high: '40%' }[st.position];
    hook.style.top = { high: '9%', mid: '18.5%', low: '28%' }[st.hook_position || 'high'];
  }
  function loadStyle() {
    if (!$('st-font').options.length) {
      styleFonts.forEach(function (f) { var o = document.createElement('option'); o.value = f; o.textContent = f; o.style.fontFamily = f; $('st-font').appendChild(o); });
      STYLE_PRESETS.forEach(function (p) {
        var b = document.createElement('button'); b.type = 'button'; b.className = 'preset'; b.textContent = p.name;
        b.style.background = p.box; b.style.color = p.text; b.style.fontFamily = "'" + p.font + "', sans-serif";
        b.onclick = function () { writeStyle(p); }; $('presets').appendChild(b);
      });
      ['st-font', 'st-box', 'st-text', 'st-hook-box', 'st-hook-text', 'st-pos', 'st-hook-pos', 'st-corners', 'st-look'].forEach(function (id) { $(id).oninput = preview; $(id).onchange = preview; });
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
        var ids = [], names = [], thumbs = [], secs = [];
        try { ids = JSON.parse(r.clip_ids || '[]') || []; } catch (e) { ids = []; }
        try { names = JSON.parse(r.clip_names || '[]') || []; } catch (e) { names = []; }
        try { thumbs = JSON.parse(r.clip_thumbs || '[]') || []; } catch (e) { thumbs = []; }
        try { secs = JSON.parse(r.clip_sections || '[]') || []; } catch (e) { secs = []; }
        // a call stuck in Editing (engine restarted under it) can be restarted after an hour
        var stuck = r.status === 'editing' && (Date.now() - new Date(r.uploaded_at).getTime()) > 3600000;
        var tiles = ids.map(function (id, i) {
          var nm = String(names[i] || 'Clip ' + (i + 1)).split(' - 20')[0];
          return '<div class="cliptile" data-id="' + esc(id) + '">' +
            '<div class="thumbwrap"' + (thumbs[i] ? ' data-thumb="' + esc(thumbs[i]) + '"' : '') + '><img loading="lazy" alt="" src="https://drive.google.com/thumbnail?id=' + esc(id) + '&sz=w400" onerror="this.parentNode.classList.add(\'noimg\')"><span class="play">▶</span></div>' +
            '<b>' + esc(nm) + '</b>' + (secs[i] ? '<small class="sec">' + esc(secs[i]) + '</small>' : '') +
            '<div class="tile-btns"><a class="btn tiny dlbtn" href="https://drive.google.com/uc?export=download&id=' + esc(id) + '" target="_blank" rel="noopener">⬇ DOWNLOAD</a></div></div>';
        }).join('');
        return '<div class="item logrow' + (ids.length ? ' has-clips' : '') + '" data-call="' + esc(r.call_id) + '"><div>' +
          '<div class="t">' + (ids.length ? '<span class="arrow">▶</span> ' : '') + esc(r.username) + ' · ' + esc(fmtDate(r.uploaded_at)) + '</div>' +
          '<div class="m">' + esc(r.call_id) + ' · video ' + (r.video_MB || 0) + ' MB · audio ' + (r.audio_MB || 0) + ' MB' + (r.note ? ' · ' + esc(r.note) : '') +
          (r.problem ? ' · ' + esc(r.problem) : '') + '</div></div><span class="pill ' + esc(st) + '">' + esc(st) + (r.clips_delivered ? ' · ' + r.clips_delivered + ' clips' : '') + '</span>' +
          (r.status === 'problem' || stuck ? '<button class="btn small" data-retry="' + esc(r.call_id) + '">' + (stuck ? 'RESTART' : 'RETRY') + '</button>' : '') +
          (ids.length ? '<div class="log-clips hidden"><div class="ub-grid">' + tiles + '</div>' +
            (r.clip_folder ? '<a class="btn tiny" href="https://drive.google.com/drive/folders/' + esc(r.clip_folder) + '" target="_blank" rel="noopener">📁 OPEN FOLDER</a>' : '') + '</div>' : '') +
          '</div>';
      }).join('');
      // click a row to drop its clips open; previews load the first time, same as My Clips
      el.querySelectorAll('.logrow.has-clips').forEach(function (row) {
        row.querySelector('.t').addEventListener('click', function () {
          var box = row.querySelector('.log-clips'); box.classList.toggle('hidden'); row.classList.toggle('open');
          if (box.classList.contains('hidden') || row.dataset.loaded) return;
          row.dataset.loaded = '1';
          if (!box.querySelector('[data-thumb]')) return;
          api({ action: 'adminClipThumbs', callId: row.dataset.call }).then(function (r) {
            var map = (r && r.thumbs) || {};
            box.querySelectorAll('.thumbwrap[data-thumb]').forEach(function (w) {
              var d = map[w.dataset.thumb];
              if (d && /^data:image\//.test(d)) { w.querySelector('img').src = d; w.classList.remove('noimg'); }
            });
          }).catch(function () {});
        });
      });
      el.querySelectorAll('.logrow .cliptile').forEach(function (t) {
        t.addEventListener('click', function (e) {
          if (e.target.closest('.dlbtn')) return;
          window.open('https://drive.google.com/file/d/' + t.dataset.id + '/view', '_blank', 'noopener');
        });
      });
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
  function fmtDateET(d) {
    if (!d) return ''; var t = new Date(d); if (isNaN(t)) return String(d);
    try { return t.toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }); }
    catch (e) { return fmtDate(d); }
  }

  // ---------------------------------------------------------------- team leaderboard
  function loadTeamBoard() {
    api({ action: 'teamBoard' }).then(function (j) {
      var rows = (j.board || []).filter(function (r) { return r.calls > 0; });
      if (!rows.length) { $('team-board').classList.add('hidden'); return; }
      var medal = ['🥇', '🥈', '🥉'];
      $('tb-podium').innerHTML = rows.slice(0, 3).map(function (r, i) {
        return '<div class="pod p' + (i + 1) + '"><div class="pod-medal">' + medal[i] + '</div>' +
          '<div class="pod-name">' + esc(String(r.name).split(' ')[0].toUpperCase()) + '</div>' +
          '<div class="pod-score">' + r.calls + ' CALL' + (r.calls === 1 ? '' : 'S') + '</div>' +
          '<div class="pod-sub">' + r.clips + ' clip' + (r.clips === 1 ? '' : 's') + '</div></div>';
      }).join('');
      $('tb-rest').innerHTML = rows.slice(3, 10).map(function (r, i) {
        return '<div class="tb-row"><b>' + (i + 4) + '.</b> ' + esc(String(r.name).split(' ')[0].toUpperCase()) +
          ' <small>' + r.calls + ' call' + (r.calls === 1 ? '' : 's') + ' · ' + r.clips + ' clip' + (r.clips === 1 ? '' : 's') + '</small></div>';
      }).join('');
      $('team-board').classList.remove('hidden');
    }).catch(function () {});
  }

  // ---------------------------------------------------------------- admin posted board
  function loadPosted() {
    var ag = $('posted-agents'), hk = $('posted-hooks');
    ag.innerHTML = '<div class="empty">Loading…</div>'; hk.innerHTML = '';
    api({ action: 'adminPosted' }).then(function (j) {
      var rows = j.agents || [];
      if (!rows.length) { ag.innerHTML = '<div class="empty">No clips delivered in the last 30 days.</div>'; }
      else ag.innerHTML = rows.map(function (a) {
        var pct = a.delivered ? Math.round(a.posted / a.delivered * 100) : 0;
        return '<div class="item"><div style="flex:1"><div class="t">' + esc(a.display_name) + '</div>' +
          '<div class="m">' + a.posted + ' posted of ' + a.delivered + ' delivered (' + pct + '%)' +
          (a.delivered - a.posted > 0 ? ' · <b>' + (a.delivered - a.posted) + ' waiting in Drive</b>' : ' · all posted 🎉') + '</div>' +
          '<div class="ubar"><i style="width:' + Math.max(4, pct) + '%"></i></div></div></div>';
      }).join('');
      hk.innerHTML = (j.hooks || []).length ? j.hooks.map(function (h) {
        return '<div class="item"><div><div class="t">🎬 ' + esc(h.hook) + '</div><div class="m">posted by ' + esc(h.agent) + '</div></div></div>';
      }).join('') : '<div class="empty">Nothing marked as posted yet — the buttons are on every clip in My Clips.</div>';
    }).catch(function (err) { ag.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  }
  if ($('reload-posted')) $('reload-posted').onclick = loadPosted;

  // ---------------------------------------------------------------- inside the theater (easter egg)
  var cinema = { list: [], i: 0 };
  function cinemaShow() {
    var s = $('cin-screen');
    if (!cinema.list.length) {
      s.innerHTML = '<div class="cin-empty">🎬 NOW SHOWING: YOUR CLIPS<br>The first ones premiere here as soon as a call is delivered.</div>';
      $('cin-title').textContent = 'COMING SOON';
      $('cin-prev').disabled = true; $('cin-next').disabled = true;
      return;
    }
    var c = cinema.list[cinema.i];
    s.innerHTML = '<iframe src="https://drive.google.com/file/d/' + esc(c.id) + '/preview" allow="autoplay; fullscreen" allowfullscreen title="A clip"></iframe>';
    $('cin-title').textContent = 'NOW PLAYING ' + (cinema.i + 1) + '/' + cinema.list.length + ' · ' + c.name.toUpperCase() +
      (c.agent ? ' — BY ' + String(c.agent).split(' ')[0].toUpperCase() : '');
    $('cin-prev').disabled = cinema.i === 0;
    $('cin-next').disabled = cinema.i >= cinema.list.length - 1;
  }
  function openCinema() {
    $('cinema').classList.remove('hidden'); blip();
    cinema.list = []; cinema.i = 0;
    $('cin-screen').innerHTML = '<div class="cin-empty">🎞 LOADING TONIGHT\'S SHOWINGS…</div>';
    $('cin-title').textContent = 'EDITOR ISLAND CINEMA';
    var build = function (calls) {                         // a shuffled reel of this agent's own clips from the last 2 weeks
      var cutoff = Date.now() - 14 * 86400000;
      var pool = [];
      (calls || []).forEach(function (c) {
        var t = new Date(c.uploaded_at).getTime();
        if (!(t > cutoff)) return;
        var ids = [], names = [];
        try { ids = JSON.parse(c.clip_ids || '[]') || []; } catch (e) { ids = []; }
        try { names = JSON.parse(c.clip_names || '[]') || []; } catch (e) { names = []; }
        ids.forEach(function (id, i) {
          if (id) pool.push({ id: id, name: String(names[i] || 'Clip ' + (i + 1)).split(' - 20')[0] });
        });
      });
      for (var i = pool.length - 1; i > 0; i--) { var j = Math.floor(Math.random() * (i + 1)); var t2 = pool[i]; pool[i] = pool[j]; pool[j] = t2; }
      cinema.list = pool.slice(0, 12);
      cinemaShow();
    };
    if (lastCalls) build(lastCalls);
    else api({ action: 'myCalls' }).then(function (j) { lastCalls = j.calls; build(j.calls); }).catch(function () { build([]); });
  }
  function closeCinema() { $('cinema').classList.add('hidden'); $('cin-screen').innerHTML = ''; }
  $('isl-doors').onclick = openCinema;
  $('isl-usher').onclick = openCinema;
  $('cin-exit').onclick = closeCinema;
  $('cin-prev').onclick = function () { if (cinema.i > 0) { cinema.i--; cinemaShow(); } };
  $('cin-next').onclick = function () { if (cinema.i < cinema.list.length - 1) { cinema.i++; cinemaShow(); } };
  $('cinema').addEventListener('click', function (e) { if (e.target === $('cinema')) closeCinema(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !$('cinema').classList.contains('hidden')) closeCinema(); });

  applySession();
})();
