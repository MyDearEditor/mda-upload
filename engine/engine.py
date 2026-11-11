#!/usr/bin/env python3
"""My Dear Academy editing engine.
Polls the Apps Script doorman, downloads each uploaded call from Mason's Drive, syncs the dialer audio to the
phone video, transcribes, asks Claude for clips + redactions, renders Reels, re-checks compliance, uploads the
survivors to the agent's folder and reports back. No Google login lives here: Drive access is a short-lived
token handed out by the doorman.

Env (see /opt/mda/.env): MDA_API_URL, MDA_ENGINE_SECRET, ANTHROPIC_API_KEY,
optional: CLAUDE_MODEL, WHISPER_MODEL, MDA_WORK_DIR, MDA_POLL_SECONDS
"""
import os, sys, json, time, re, shutil, subprocess, logging, datetime, difflib
from zoneinfo import ZoneInfo
import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from render import render, predict_duration  # noqa: E402
import script_map as sm                     # noqa: E402  where in the sales script the agent is
try:                                    # a missing reportlab must never take the whole engine down;
    import caption_pdf                  # without it every clip still ships, with a .txt caption
except Exception as _ex:                # noqa: E402
    caption_pdf = None
    print('caption_pdf unavailable (%s) - captions will be delivered as .txt' % _ex)

API = os.environ['MDA_API_URL']
SECRET = os.environ['MDA_ENGINE_SECRET']
MODEL = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-5')
WHISPER = os.environ.get('WHISPER_MODEL', 'small.en')
WORK = os.environ.get('MDA_WORK_DIR', '/opt/mda/work')
POLL = int(os.environ.get('MDA_POLL_SECONDS', '120'))
MIN_SYNC_CONFIDENCE = 8.0

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('mda')

PROHIBITED = """1. Client health information: conditions, diagnoses, medications, doctors, hospitals, surgeries,
   height/weight, tobacco use, answers to health questions, reasons for a waiting period.
2. Carrier names: ANY life insurance company (AIG, Corebridge, Mutual of Omaha, GTL, Guaranteed Trust Life,
   Americo, Aetna, CVS, Royal Neighbors, Transamerica, Foresters, Gerber, Colonial Penn, Liberty Bankers, SBLI,
   Prosperity, and any other), including nicknames and carrier product names.
3. Client personal names: the client, spouse, family members, beneficiaries.
4. Locations: cities, states, counties, street addresses, zip codes.
5. Contact details: phone numbers, emails, social media handles.
6. Financial or ID details: bank names, account/routing numbers, card numbers, SSN, Medicare/Medicaid numbers,
   dates of birth, payment draft dates tied to a bank.
ALLOWED: the agent's own name, "Legacy Family Life", "My Dear Academy", and generic terms such as
"whole life", "final expense", "two-year waiting period", "coverage", "premium"."""


# ---------------------------------------------------------------- doorman
def api(action, **kw):
    kw.update(action=action, secret=SECRET)
    r = requests.post(API, data=json.dumps(kw), headers={'Content-Type': 'text/plain'}, timeout=90)
    r.raise_for_status()
    j = r.json()
    if not j.get('ok'):
        raise RuntimeError(f'{action}: {j.get("error")}')
    return j


class Token:
    def __init__(self): self.t, self.at = None, 0
    def get(self):
        if not self.t or time.time() - self.at > 45 * 60:
            self.t, self.at = api('engineToken')['token'], time.time()
        return self.t


TOKEN = Token()


def drive_download(file_id, dest):
    with requests.get(f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true',
                      headers={'Authorization': 'Bearer ' + TOKEN.get()}, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def drive_update(file_id, path, mime):
    """Replace an existing Drive file's content in place - same id, same name, same links."""
    with open(path, 'rb') as f:
        r = requests.patch(f'https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media&supportsAllDrives=true',
                           headers={'Authorization': 'Bearer ' + TOKEN.get(), 'Content-Type': mime},
                           data=f, timeout=600)
    r.raise_for_status()
    return file_id


def drive_find(name, parent_id):
    """File id of `name` inside a folder, or None."""
    q = f"name = '{name}' and '{parent_id}' in parents and trashed = false"
    r = requests.get('https://www.googleapis.com/drive/v3/files',
                     params={'q': q, 'fields': 'files(id)', 'supportsAllDrives': True,
                             'includeItemsFromAllDrives': True},
                     headers={'Authorization': 'Bearer ' + TOKEN.get()}, timeout=60)
    r.raise_for_status()
    files = r.json().get('files', [])
    return files[0]['id'] if files else None


def drive_upload(path, name, parent_id, mime):
    size = os.path.getsize(path)
    r = requests.post('https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true',
                      headers={'Authorization': 'Bearer ' + TOKEN.get(), 'Content-Type': 'application/json; charset=UTF-8',
                               'X-Upload-Content-Type': mime, 'X-Upload-Content-Length': str(size)},
                      data=json.dumps({'name': name, 'parents': [parent_id]}), timeout=60)
    r.raise_for_status()
    url = r.headers['Location']
    CH = 8 * 1024 * 1024
    with open(path, 'rb') as f:
        off = 0
        while off < size:
            data = f.read(CH)
            end = off + len(data) - 1
            for attempt in range(5):
                p = requests.put(url, data=data, headers={'Content-Range': f'bytes {off}-{end}/{size}'}, timeout=600)
                if p.status_code in (200, 201):
                    return p.json()['id']
                if p.status_code == 308:
                    break
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f'upload failed at byte {off}: {p.status_code} {p.text[:200]}')
            off = end + 1
    raise RuntimeError('upload ended without a file id')


def clip_thumb(clip_path, out_dir, i, parent_id):
    """A real photo for the site's clip gallery: one frame from the clip, 480px wide."""
    try:
        tp = os.path.join(out_dir, f'thumb{i}.jpg')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '1', '-i', clip_path,
                        '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '5', tp], check=True)
        return drive_upload(tp, f'preview{i}.jpg', parent_id, 'image/jpeg')
    except Exception as ex:
        log.warning('thumbnail for clip %s failed: %s', i, ex)
        return ''


def drive_mkdir(name, parent_id):
    """Create a subfolder under parent_id and return its id."""
    r = requests.post('https://www.googleapis.com/drive/v3/files?supportsAllDrives=true',
                      headers={'Authorization': 'Bearer ' + TOKEN.get(), 'Content-Type': 'application/json; charset=UTF-8'},
                      data=json.dumps({'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}),
                      timeout=60)
    r.raise_for_status()
    return r.json()['id']


# ---------------------------------------------------------------- names
ET = ZoneInfo('America/New_York')


def uploaded_et(call):
    """uploaded_at as Eastern time. The doorman stores UTC, so a 9pm ET upload reads as the next day
    until it is converted — that is what this fixes."""
    s = str(call.get('uploaded_at', '') or '').strip()
    dt = None
    if s:
        try:
            dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M'):
                try:
                    dt = datetime.datetime.strptime(s[:len(datetime.datetime.now().strftime(fmt))], fmt)
                    break
                except ValueError:
                    continue
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(ET)


def when_label(call):
    """'2026-10-26 - 6.42 PM ET' — the date and Eastern time every name and the clip sheet share."""
    d = uploaded_et(call)
    return '%04d-%02d-%02d - %d.%02d %s ET' % (d.year, d.month, d.day, d.hour % 12 or 12, d.minute,
                                               'AM' if d.hour < 12 else 'PM')


def safe_name(s, limit=70):
    """Trim to something Drive, Windows and macOS are all happy with."""
    s = re.sub(r'[\\/:*?"<>|]', ' ', str(s or ''))
    s = re.sub(r'\s+', ' ', s).strip(' .')
    return s[:limit].strip(' .')


def call_folder_name(call):
    """This call's delivery subfolder: the call name the agent typed, then the date and Eastern time,
    e.g. 'Facebook lead - 2026-10-26 - 6.42 PM ET'."""
    return (safe_name(call.get('note'), 60) or 'Call') + ' - ' + when_label(call)


# ---------------------------------------------------------------- media
def wav8k(src, dst):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-ac', '1', '-ar', '8000', '-vn', dst], check=True)
    import scipy.io.wavfile as w
    _, x = w.read(dst)
    return x.astype(np.float32)


def synced_full_video(video, audio, offset, a_min, a_max, out, drift=0.0):
    """Original phone video (no re-encode) with the clean call audio replacing the phone audio, trimmed to the overlap.

    offset is the value at the middle of [a_min, a_max]. When the two clocks drift, the audio is
    time-stretched by the measured factor (a few hundred ppm - inaudible) so it stays locked to the
    video across the whole call instead of only at the middle."""
    mid = (a_min + a_max) / 2.0
    v0 = a_min + offset + drift * (a_min - mid)   # offset AT a_min, so the stretch anchors correctly
    dur = a_max - a_min
    cmd = ['ffmpeg', '-v', 'error', '-y', '-ss', f'{v0:.3f}', '-t', f'{dur * (1 + drift):.3f}', '-i', video,
           '-ss', f'{a_min:.3f}', '-t', f'{dur:.3f}', '-i', audio,
           '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k']
    if abs(drift) > 5e-5:
        cmd += ['-af', f'atempo={1.0 / (1.0 + drift):.8f}']
    subprocess.run(cmd + ['-shortest', '-movflags', '+faststart', out], check=True)
    return out


def media_duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout.strip()
    try: return float(out)
    except ValueError: return 0.0


def _envelope(x, hop=160):
    """Loudness envelope, one point per 20 ms: robust to telephony filtering and weak phone-mic pickup."""
    n = len(x) // hop * hop
    if n == 0:
        return np.zeros(1, np.float64)
    e = np.sqrt((x[:n].astype(np.float64) ** 2).reshape(-1, hop).mean(axis=1))
    e = np.log1p(e * 300.0)
    return e - e.mean()


_ENVS = {}


def local_refine(off, a0, a1, max_shift=2.5):
    """Re-correlate ONE clip's stretch of the recording against the video audio, searching only a
    small window around the global offset. A recorder that skips is only locally consistent -
    each clip finds its own exact alignment and the skips between clips stop mattering."""
    env = _ENVS.get('env')
    if env is None:
        return off
    try:
        import scipy.signal as s
        EPS = 50
        ev, ea = env
        i0, i1 = max(0, int(a0 * EPS)), int(a1 * EPS)
        seg = ea[i0:i1]
        if len(seg) < 8 * EPS:
            return off
        seg = seg - seg.mean()
        cs = s.correlate(ev, seg, mode='full', method='fft')
        base = len(seg) - 1 + i0
        k0 = max(0, base + int((off - max_shift) * EPS))
        k1 = min(len(cs), base + int((off + max_shift) * EPS))
        if k1 - k0 < EPS:
            return off
        k = k0 + int(np.argmax(cs[k0:k1]))
        return (k - base) / float(EPS)
    except Exception:
        return off


def sync_offset(video, audio, tmp):
    """Returns (offset_seconds, confidence, detail). Audio t=0 sits at video t=offset.
    Whole-file envelope correlation (no 6-minute cap, works with long pre-roll and headphone calls),
    gated by an agreement check: the first and second halves of the call recording must independently
    land on the same offset. Then a sample-accurate refine on raw audio."""
    import scipy.signal as s
    HOP = 160                                     # 20 ms at 8 kHz
    v = wav8k(video, os.path.join(tmp, 'v.wav'))
    a = wav8k(audio, os.path.join(tmp, 'a.wav'))
    v_rms = float(np.sqrt(np.mean(v.astype(np.float64) ** 2)))
    a_rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
    if v_rms < 1.0:
        return 0.0, 0.0, 'video has no usable audio track'
    if a_rms < 1.0:
        return 0.0, 0.0, 'call recording is silent'

    ev, ea = _envelope(v, HOP), _envelope(a, HOP)
    _ENVS.clear(); _ENVS['env'] = (ev, ea)        # kept for the per-clip local re-align
    c = s.correlate(ev, ea, mode='full', method='fft')
    k = int(np.argmax(c))
    lag = k - (len(ea) - 1)
    peak_z = float((c[k] - np.median(c)) / (np.std(c) + 1e-9))

    adur, vdur = len(a) / 8000.0, len(v) / 8000.0
    EPS = 8000 // HOP                             # envelope points per second (50)

    misses = []                                   # (candidate, halves-difference) for the log

    def halves_agree_at(off_s):
        """Validate a candidate offset using ONLY the stretch of the recording that overlaps the video:
        both halves of that overlap must independently land on (nearly) the same offset. This also
        works when the video is just a short snippet of a much longer call.

        The tolerance grows with call length: the phone camera and the dialer recorder run on
        different clocks, and a drift of a few hundred ppm - normal for phone recordings - moves the
        halves of a 40-minute call ~half a second apart even when the match is real. A fixed 0.35 s
        gate was rejecting exactly those calls. The renderer re-refines the offset locally around
        every clip, so a midpoint-averaged offset is all a long, slightly drifting call needs."""
        o0, o1 = max(0.0, -off_s), min(adur, vdur - off_s)
        olap = o1 - o0
        if olap < 10:                             # too little shared time to judge (also rejected later)
            return None
        seg_need = min(15.0, max(4.0, olap / 2 - 1))   # short pairs get judged on shorter halves
        tol = 0.35 + 0.0005 * (olap / 2)          # 0.35 s plus 500 ppm of the half-center gap
        mid = (o0 + o1) / 2.0
        got = []
        for s0, s1 in ((o0, mid), (mid, o1)):
            i0 = int(s0 * EPS)
            seg = ea[i0:int(s1 * EPS)]
            if len(seg) < seg_need * EPS:
                return None
            seg = seg - seg.mean()
            cs = s.correlate(ev, seg, mode='full', method='fft')
            got.append(((int(np.argmax(cs)) - (len(seg) - 1)) - i0) / float(EPS))
        if abs(got[0] - got[1]) <= tol:
            return (got[0] + got[1]) / 2.0
        misses.append((off_s, abs(got[0] - got[1])))
        return None

    fitnotes = []                                 # per-segment offsets of a failed fit, for the log

    def drift_fit_at(off_s):
        """The halves disagreeing does not always mean different calls: it can mean two clocks at
        different speeds, or a recorder that paused. Split the overlap into eighths and correlate
        each on its own - a real match puts the offsets on a straight line (the slope IS the
        drift); a paused recorder gives line segments; two different calls scatter. The longest
        contiguous run of segments that agree on one line wins, and the edit is confined to the
        stretch of the call that run covers. Returns (offset at that stretch's midpoint, drift,
        window start, window end) or None."""
        o0, o1 = max(0.0, -off_s), min(adur, vdur - off_s)
        olap = o1 - o0
        if olap < 120:                            # drift is invisible on short calls
            return None
        N = 8 if olap >= 320 else 4
        # each segment searches only a drift-sized window around the candidate: the candidate came
        # from a strong peak, and an open search lets a quiet segment latch onto a false peak far away
        W = 0.005 * olap / 2 + 1.5                # the widest offset swing a plausible drift allows
        pts = []
        for q in range(N):
            s0, s1 = o0 + olap * q / N, o0 + olap * (q + 1) / N
            i0 = int(s0 * EPS)
            seg = ea[i0:int(s1 * EPS)]
            if len(seg) < 25 * EPS:
                return None
            seg = seg - seg.mean()
            cs = s.correlate(ev, seg, mode='full', method='fft')
            base = len(seg) - 1 + i0              # cs index whose lag is offset 0
            k0 = max(0, base + int((off_s - W) * EPS))
            k1 = min(len(cs), base + int((off_s + W) * EPS))
            if k1 - k0 < EPS:
                return None
            k = k0 + int(np.argmax(cs[k0:k1]))
            pts.append(((s0 + s1) / 2.0, (k - base) / float(EPS)))
        # consensus line: every pair proposes one; the best is the one most points sit on
        best = None
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                sl = (pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0])
                if abs(sl) > 0.005:
                    continue
                ic = pts[i][1] - sl * pts[i][0]
                tol_in = max(0.4, abs(sl) * olap / 8)
                inl = [q for q in range(len(pts)) if abs(pts[q][1] - (sl * pts[q][0] + ic)) <= tol_in]
                if best is None or len(inl) > len(best):
                    best = inl
        fitnotes.append((off_s, ' '.join(f'{p[1]:+.2f}' for p in pts)))
        if best is None:
            return None
        # the window we trust is the longest CONTIGUOUS run of agreeing segments - with a paused
        # recorder the call splits into regions, and mixing them would misalign half the clips
        runs, cur = [], []
        for q in range(len(pts)):
            if q in best:
                cur.append(q)
            else:
                if cur: runs.append(cur)
                cur = []
        if cur: runs.append(cur)
        run = max(runs, key=len) if runs else []
        if len(run) < max(3, N // 2):             # trust nothing shorter than half the call
            # a recorder that skips every few minutes never fits ONE line, but its segments still
            # huddle in a tight band around the true offset. Take the longest contiguous stretch
            # inside that band - the per-clip local re-align absorbs the wobble within it.
            med = float(np.median([p[1] for p in pts]))
            inb = [q for q in range(len(pts)) if abs(pts[q][1] - med) <= 1.0]
            runs2, cur2 = [], []
            for q in range(len(pts)):
                if q in inb:
                    cur2.append(q)
                else:
                    if cur2: runs2.append(cur2)
                    cur2 = []
            if cur2: runs2.append(cur2)
            run = max(runs2, key=len) if runs2 else []
            if len(run) < max(3, N // 2):
                return None
        rp = [pts[q] for q in run]
        ts = np.array([p[0] for p in rp]); offs = np.array([p[1] for p in rp])
        slope, icpt = (np.polyfit(ts, offs, 1) if len(rp) > 1 else (0.0, offs[0]))
        if abs(slope) > 0.005:
            return None
        w0 = o0 + olap * run[0] / N
        w1 = o0 + olap * (run[-1] + 1) / N
        mid = (w0 + w1) / 2.0
        fitnotes.pop()                            # it worked; no note needed
        return float(slope * mid + icpt), float(slope), float(w0), float(w1)

    # candidate offsets: the global peak, plus what each half of the whole recording suggests on its own
    # (with a snippet video the global peak can land in the wrong place; one of these usually has it)
    half = len(ea) // 2
    cands = [lag / float(EPS)]
    for h0, seg in ((0, ea[:half]), (half, ea[half:])):
        seg = seg - seg.mean()
        if len(seg) >= 1500:
            cs = s.correlate(ev, seg, mode='full', method='fft')
            cands.append(((int(np.argmax(cs)) - (len(seg) - 1)) - h0) / float(EPS))
    off, agree, tried, drift = cands[0], False, [], 0.0
    for cand in cands:
        if any(abs(cand - t) < 1.0 for t in tried):
            continue
        tried.append(cand)
        got = halves_agree_at(cand)
        if got is not None:
            off, agree = got, True
            break
    win = None
    if not agree:
        for cand in tried:                        # second look: is this a drifting clock, not a mismatch?
            fit = drift_fit_at(cand)
            if fit is not None:
                off, drift, agree = fit[0], fit[1], True
                win = (fit[2], fit[3])
                break
    detail = ((f'overlap halves agree at {off:+.1f}s' if not drift and not win else
               f'drift fit at {off:+.1f}s mid, {drift * 1e6:+.0f} ppm, window {win[0]:.0f}-{win[1]:.0f}s') if agree else
              'no overlap agreement (' + ', '.join(f'{t:+.1f}s' for t in tried) +
              ('; halves differ by ' + ', '.join(f'{d:.2f}s at {t:+.1f}s' for t, d in misses) if misses else '') +
              ('; seg offsets ' + '; '.join(f'[{p}] at {t:+.1f}s' for t, p in fitnotes) if fitnotes else '') + ')')
    # sample-accurate refine on a 20 s window in the middle of the overlap (or the trusted window)
    adur, vdur = len(a) / 8000.0, len(v) / 8000.0
    o0, o1 = max(0.0, -off), min(adur, vdur - off)
    if win:
        o0, o1 = max(o0, win[0]), min(o1, win[1])
    if o1 - o0 > 12:
        W = min(20.0, o1 - o0 - 4)
        ta = max(o0 + 1, (o0 + o1) / 2 - W / 2)
        ca = a[int(ta * 8000):int((ta + W) * 8000)].astype(np.float64)
        tv0 = max(0.0, ta + off - 1.0)
        va = v[int(tv0 * 8000):int((tv0 + W + 2.0) * 8000)].astype(np.float64)
        if len(va) > len(ca) > 8000 * 4:
            cc = s.correlate(va - va.mean(), ca - ca.mean(), mode='valid')
            off = (tv0 + float(np.argmax(cc)) / 8000.0) - ta
    # agreement across independent windows is the real gate; the peak score is kept for the logs
    conf = max(peak_z, MIN_SYNC_CONFIDENCE) if agree else 0.0
    return float(off), conf, f'peak {peak_z:.1f}, {detail}', float(drift), win


# Calibrated against a real headphone call (separation 1.66-1.83, coherence 80-82%) and a synthetic
# speakerphone (separation 1.28, coherence 48%). Separation alone cannot tell those apart - real speech
# is too variable within one speaker - so turn coherence is the gate that decides and separation is only
# a floor against degenerate splits. spkcheck.py prints both for any call, to recalibrate from evidence.
SPK_MIN_SEP = 1.00        # a floor, not the real test
SPK_MIN_SHARE = 0.12      # each speaker must hold at least this share of words for the split to mean anything
SPK_MIN_RUNS = 0.70       # the decider: real turns hold together, noise flips word to word


def speaker_labels(vwav, awav, offset, words):
    """Work out who is speaking, from the two recordings we already have.

    The phone's own mic hears the agent in the room; the dialer hears both sides evenly. So a word that
    is loud in BOTH is the agent, and a word loud only in the call recording is the client. The sync
    offset is already known to within a fraction of a second, so the two can be compared word by word.

    Speakerphone breaks this - the client comes out of the handset and back into the room mic - so the
    separation between the two groups is measured and reported rather than assumed.

    Tags each word dict in place with w['spk'] = 'agent'|'client' and returns (ok, confidence, detail).
    Tagging the words themselves rather than returning indices keeps this safe when callers pass a
    filtered subset of the transcript around.
    """
    import scipy.io.wavfile as wav
    try:
        _, v = wav.read(vwav)
        _, a = wav.read(awav)
    except Exception as ex:
        return False, 0.0, 'could not read the aligned audio (%s)' % ex
    for wd in words:
        wd.pop('spk', None)
    v = v.astype(np.float64)
    a = a.astype(np.float64)
    SR = 8000
    vals, idx = [], []
    for n, wd in enumerate(words):
        s, e = float(wd['s']), float(wd['e'])
        if e - s < 0.06:
            continue
        ai0, ai1 = int(s * SR), int(e * SR)
        vi0, vi1 = int((s + offset) * SR), int((e + offset) * SR)
        if ai0 < 0 or vi0 < 0 or ai1 > len(a) or vi1 > len(v) or ai1 <= ai0 or vi1 <= vi0:
            continue
        arms = float(np.sqrt(np.mean(a[ai0:ai1] ** 2))) + 1e-6
        vrms = float(np.sqrt(np.mean(v[vi0:vi1] ** 2))) + 1e-6
        vals.append(np.log(vrms / arms))      # how much louder the room mic is than the call, per word
        idx.append(n)
    if len(vals) < 40:
        return False, 0.0, 'not enough clean words to tell the voices apart'

    # two clusters on one dimension: the room-mic-hot group is the agent
    x = np.asarray(vals)
    lo, hi = float(np.percentile(x, 20)), float(np.percentile(x, 80))
    grp = x > (lo + hi) / 2.0
    for _ in range(40):
        if grp.all() or not grp.any():
            break
        lo, hi = float(x[~grp].mean()), float(x[grp].mean())
        new = np.abs(x - hi) < np.abs(x - lo)
        if np.array_equal(new, grp):
            break
        grp = new
    if grp.all() or not grp.any():
        return False, 0.0, 'only one voice is audible on the phone video'
    spread = float(x[~grp].std() + x[grp].std()) + 1e-6
    sep = (float(x[grp].mean()) - float(x[~grp].mean())) / spread
    share = float(grp.mean())

    # people take turns: consecutive words close in time should mostly carry the same label. A split
    # that flips word to word is the clustering finding noise, however far apart the two means look.
    pairs = same = 0
    for k in range(1, len(idx)):
        if float(words[idx[k]]['s']) - float(words[idx[k - 1]]['e']) <= 1.5:
            pairs += 1
            same += bool(grp[k] == grp[k - 1])
    runs = (same / pairs) if pairs else 0.0

    detail = 'separation %.2f, turn coherence %.0f%%, agent holds %.0f%% of words' % (sep, runs * 100, share * 100)
    if sep < SPK_MIN_SEP or runs < SPK_MIN_RUNS or not (SPK_MIN_SHARE <= share <= 1 - SPK_MIN_SHARE):
        return False, sep, detail + ' - too close to call (speakerphone or a noisy room?)'
    for k in range(len(idx)):
        words[idx[k]]['spk'] = 'agent' if grp[k] else 'client'
    return True, sep, detail


def label_transcript(transcript, agent_name):
    """Transcript lines prefixed with who said them, for the clip picker to read."""
    out = []
    for sg in transcript:
        tags = [w.get('spk') for w in sg['words'] if w.get('spk')]
        who = agent_name.upper() if tags.count('agent') >= tags.count('client') else 'CLIENT'
        out.append('[%.1f-%.1f] %s: %s' % (sg['start'], sg['end'], who, sg['text'].strip()))
    return out


def agent_turns(transcript, start, end, join=0.8):
    """Stretches of audio time where the agent is the one talking, for the push-in."""
    ws = [w for sg in transcript for w in sg['words']
          if w.get('spk') and w['e'] > start and w['s'] < end]
    turns, cur = [], None
    for w in sorted(ws, key=lambda x: x['s']):
        if w['spk'] != 'agent':
            continue
        if cur and w['s'] - cur[1] <= join:
            cur[1] = w['e']
        else:
            if cur:
                turns.append(cur)
            cur = [w['s'], w['e']]
    if cur:
        turns.append(cur)
    return [(max(start, s), min(end, e)) for s, e in turns if min(end, e) > max(start, s)]


def mmss(seconds):
    s = int(round(max(0.0, float(seconds))))
    return '%d:%02d' % (s // 60, s % 60)


def sync_problem(detail, offset, vdur, adur):
    """Say what is actually wrong with the pair, not just that it would not line up.

    'Please re-upload the matching pair' sends an agent looking for a file that is usually not the
    problem. Far more often one of the two recordings is short - the dialer caught the tail of the
    call, or the camera was only rolling for part of it - and no amount of re-uploading fixes that.
    """
    if 'no usable audio' in detail:
        return ('Your phone video has no usable audio track. We line the two files up by their sound, '
                'so we need to be able to hear you on the video as well as on the call recording. '
                'Please check the video and upload again.')
    if 'is silent' in detail:
        return ('Your call recording is silent. We need sound in both files to line them up — please '
                'check the recording and upload again.')

    out = ['We could not line your call recording up with your phone video.',
           'Your video is %s long and your call recording is %s.' % (mmss(vdur), mmss(adur))]
    overlap = min(adur, vdur - offset) - max(0.0, -offset)
    if overlap <= 5:
        out.append('At the closest match we could find, the two barely overlap at all.')
    elif overlap < adur * 0.9:
        out.append('At the closest match we could find, only about %s of the call was on camera.' % mmss(overlap))

    if adur < vdur * 0.6:
        out.append('The recording is much shorter than the video. That is usually one of two things: your '
                   'dialer only caught part of the call, or the camera was rolling for a long time before '
                   'the call began. Anything filmed before the call starts cannot be matched or used, so '
                   'start recording when you dial.')
    elif vdur < adur * 0.6:
        out.append('The video is much shorter than the recording. If you only filmed part of the call, '
                   'there may not be enough footage to work with.')
    elif max(adur, vdur) < 60:
        out.append('Both files are quite short, and short pairs are the hardest to line up. Make sure the '
                   'camera is rolling while the call is actually happening, then upload the pair again.')
    else:
        out.append('They look like two different calls. Please check that both files are from the same '
                   'call and upload again.')
    out.append('(technical detail: %s)' % detail)
    return '\n'.join(out)


def transcribe(audio, out_json, t0=0.0, t1=None):
    """Transcribe audio between t0 and t1 (seconds of the call recording). Timestamps are in full-call time."""
    from faster_whisper import WhisperModel
    m = WhisperModel(WHISPER, device='cpu', compute_type='int8')
    cmd = ['ffmpeg', '-v', 'error', '-ss', f'{max(0.0, t0):.3f}']
    if t1 is not None: cmd += ['-t', f'{max(0.0, t1 - t0):.3f}']
    raw = subprocess.run(cmd + ['-i', audio, '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    arr = np.frombuffer(raw, np.float32)
    # the initial_prompt nudges Whisper to transcribe disfluencies verbatim so the renderer can cut them
    segs, _ = m.transcribe(arr, language='en', word_timestamps=True, vad_filter=False, beam_size=5,
                           initial_prompt="So, um, yeah, like, you know, I mean, uh, let me think.")
    out = []
    for sg in segs:
        if not sg.words: continue
        out.append({'start': sg.start + t0, 'end': sg.end + t0, 'text': sg.text,
                    'words': [{'w': w.word, 's': w.start + t0, 'e': w.end + t0} for w in sg.words]})
    json.dump(out, open(out_json, 'w'), indent=1)
    return out


# ---------------------------------------------------------------- claude
def claude(system, user, max_tokens=4000):
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                 messages=[{'role': 'user', 'content': user}])
    text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text')
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise RuntimeError('Claude returned no JSON: ' + text[:200])
    return json.loads(m.group(0))


CLIP_MIN, CLIP_MAX = 20.0, 90.0          # finished length, after every cut has been made


def map_moments(transcript, agent_name, labelled=False, spans=None):
    """Pass 1 - read the whole call and mark every moment worth studying, typed and scored.

    Deliberately NOT asked to produce finished clips: picking and framing are different jobs, and the old
    single pass did both badly. Nothing here biases toward the agent talking - an objection handled is only
    teachable if you can hear the objection."""
    if labelled:
        lines = label_transcript(transcript, agent_name)
        whose = 'Every line is marked with who said it. Trust those marks; do not second-guess them.'
    else:
        lines = [f"[{sg['start']:.1f}-{sg['end']:.1f}] {sg['text'].strip()}" for sg in transcript]
        whose = 'The lines are NOT marked with who is speaking, so work it out from context.'
    if spans:
        tags = sm.label_lines(transcript, spans)
        lines = [re.sub(r'^(\[[^\]]*\] )', r'\1' + tag, ln, count=1) for ln, tag in zip(lines, tags)]
        whose += (' Lines are also tagged with the SECTION of the sales script the agent is in (every agent '
                  'follows the same script: intro, how we work, underwriting, credibility, discovery, recap, '
                  'product, carrier, three options, the close, confirm affordability, explain the process, '
                  'the application, wrap-up). The best reels come from discovery, recap, the close, the '
                  'down-sell, confirm affordability, explain the process and the wrap-up. Never pick '
                  'underwriting or the application.')
    system = f"""You are a sales coach reviewing a recorded life-insurance call so OTHER AGENTS can learn from it.
The agent is {agent_name}; the other voice is the client. {whose} Output strict JSON only.

Find up to 8 moments worth posting. Most are moments that teach another agent something. For each:
  kind   - one of: opener, discovery, objection, close, rapport, funny
  score  - 1-10, how much an agent would actually learn. Be harsh. 9-10 is worth copying word for word,
           5 is ordinary, below 5 is not worth posting. For a funny moment, score how hard it lands
           rather than what it teaches.
  start  - where the moment begins INCLUDING its setup. If the agent is answering an objection or a
           question, the moment starts with the CLIENT saying it, never with the agent's reply.
  end    - where the thought is finished. Never end mid-thought.
  title  - 6 words or fewer: what the moment is.
  why    - 1-2 plain sentences addressed to the agent as "you": what you did, and why it worked.

A moment lasts as long as it needs. A single sharp line may be 20 seconds; an objection handled from
start to finish may be 90. Do not pad to fill time and do not cut a thought short to hit a number.
Skip anything mostly made of health questions, carrier talk, or personal or financial details.

About "funny": a call going sideways can be the most watchable thing on a recording, so it counts even
when it teaches nothing. But MOST CALLS CONTAIN NOTHING FUNNY, and returning no funny moment is the
normal, correct answer. Do not go looking for one. Awkward is not funny. Mildly odd is not funny. Only
mark one if another agent would actually laugh and send it to a colleague because they have had that
exact call themselves. The joke has to be on the situation or on the agent, never on the client - a
clip that makes the person on the other end look stupid is not usable, whatever else it is.

JSON: {{"moments":[{{"kind":"...","score":0,"start":0,"end":0,"title":"...","why":"..."}}]}}"""
    j = claude(system, 'Transcript with [start-end] seconds per line:\n\n' + '\n'.join(lines), 4000)
    out = []
    for m in j.get('moments', []):
        if not isinstance(m, dict):
            continue
        try:
            m['start'], m['end'] = float(m['start']), float(m['end'])
            m['score'] = float(m.get('score', 0))
        except (TypeError, ValueError, KeyError):
            continue
        m['kind'] = str(m.get('kind', '')).lower().strip() or 'other'
        if m['end'] > m['start']:
            out.append(m)
    return out


MIN_SCORE = 5.0                          # the picker's own scale: below 5 is not worth posting
# A kind that can be claimed for almost any stretch of a call would win its variety slot every time and
# push out a strong teaching moment. 'funny' earns its place only when it clearly lands, so it is held to
# a higher bar than the rest - and the prompt tells the picker that finding none is the normal answer.
KIND_FLOOR = {'funny': 7.0}


def overlap_s(a0, a1, b0, b1):
    """Seconds two spans share."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def choose_moments(moments, limit=5):
    """Best first - but not five versions of the same lesson. Every kind gets a slot before any repeats.

    Variety never reaches below the quality floor: a weak moment of a missing kind is worse than a second
    strong one of a kind already covered. And two moments telling mostly the same stretch of the call are
    the same moment - the lower-scored one never takes a slot."""
    ranked = sorted([m for m in moments if m['score'] >= KIND_FLOOR.get(m['kind'], MIN_SCORE)],
                    key=lambda m: -m['score'])

    def retells(m, picked):
        for p in picked:
            ov = overlap_s(m['start'], m['end'], p['start'], p['end'])
            if ov > 0.5 * min(m['end'] - m['start'], p['end'] - p['start']):
                return True
        return False

    picked, kinds = [], set()
    for m in ranked:
        if len(picked) >= limit:
            break
        if m['kind'] not in kinds and not retells(m, picked):
            picked.append(m)
            kinds.add(m['kind'])
    for m in ranked:
        if len(picked) >= limit:
            break
        if not any(m is p for p in picked) and not retells(m, picked):
            picked.append(m)
    return sorted(picked, key=lambda m: -m['score'])


def sentence_bounds(transcript, a_min, a_max):
    """[(start, end)] per sentence. Clip edges snap to these, so a clip can never end mid-word."""
    ws = [w for sg in transcript for w in sg['words'] if w['s'] >= a_min and w['e'] <= a_max]
    sents, cur = [], []
    for w in ws:
        cur.append(w)
        if re.search(r'[.?!]["\')]?$', str(w['w']).strip()):
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    bounds = [(s[0]['s'], s[-1]['e']) for s in sents if s]
    if len(bounds) < 2 and len(ws) > 30:
        # Whisper occasionally returns a stretch with no end punctuation at all. Rather than fail the
        # call, fall back to breaking on the pauses - speakers stop at the end of a thought.
        bounds, cur = [], []
        for n, w in enumerate(ws):
            cur.append(w)
            nxt = ws[n + 1] if n + 1 < len(ws) else None
            if nxt is None or nxt['s'] - w['e'] > 0.6:
                bounds.append((cur[0]['s'], cur[-1]['e']))
                cur = []
        if cur:
            bounds.append((cur[0]['s'], cur[-1]['e']))
    return bounds


def find_phrase(transcript, phrase, lo, hi):
    """Locate a spoken word sequence inside [lo, hi]: exact normalized match first, then a fuzzy
    pass that forgives a transcription slip or two. Returns (start, end) of the phrase or None."""
    want = [t for t in (_norm(x) for x in str(phrase).split()) if t]
    if len(want) < 2:
        return None
    words = []
    for sg in transcript:
        for w in sg['words']:
            t = _norm(w['w'])
            if t and lo - 2 <= w['s'] <= hi + 2:
                words.append((t, float(w['s']), float(w['e'])))
    n = len(want)
    for i in range(len(words) - n + 1):
        if [w[0] for w in words[i:i + n]] == want:
            return words[i][1], words[i + n - 1][2]
    best, best_r, tgt = None, 0.0, ' '.join(want)
    for i in range(len(words) - n + 1):
        r = difflib.SequenceMatcher(None, ' '.join(w[0] for w in words[i:i + n]), tgt).ratio()
        if r > best_r:
            best_r, best = r, (words[i][1], words[i + n - 1][2])
    return best if best_r >= 0.8 else None


def fit_span(sents, start, end, predict, lo=CLIP_MIN, hi=CLIP_MAX):
    """Snap to whole sentences, then grow or shrink until the FINISHED length lands in range.

    This is the fix for clips arriving short. The renderer only ever removes time - pauses, fillers,
    redactions - so a span chosen by how it reads always delivers less than it looks like. Here the span
    is sized by what it will actually become, measured with the renderer's own maths."""
    if not sents:
        return None
    i = min(range(len(sents)), key=lambda k: abs(sents[k][0] - start))
    j = min(range(len(sents)), key=lambda k: abs(sents[k][1] - end))
    j = max(i, j)
    best = None
    for _ in range(60):
        s, e = sents[i][0], sents[j][1]
        d = predict(s, e)
        if d is None:                       # too little left to judge - widen if we can
            if j + 1 < len(sents):
                j += 1
            elif i > 0:
                i -= 1
            else:
                return None
            continue
        best = (s, e, d)
        if d < lo:
            if j + 1 < len(sents):
                j += 1                      # grow forward first: the payoff usually follows
            elif i > 0:
                i -= 1
            else:
                break
        elif d > hi:
            if j > i:
                j -= 1
            else:
                break
        else:
            return s, e, d
    return best


def free_runs(sents, i0, j0, taken):
    """Sentence indexes i0..j0 split into contiguous runs of footage no earlier clip has claimed.

    This is what keeps two clips from ever sharing footage: a later clip may only be fitted inside one
    of these runs, so it physically cannot grow into a span an earlier (higher-ranked) clip delivered."""
    pool = [k for k in range(i0, j0 + 1)
            if not any(sents[k][0] < t1 - 0.2 and sents[k][1] > t0 + 0.2 for t0, t1 in taken)]
    runs, cur = [], []
    for k in pool:
        if cur and k != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(k)
    if cur:
        runs.append(cur)
    return runs


def caption_and_redact(transcript, spans, agent_name, sections=None):
    """Pass 2 - banner, caption and the exhaustive redaction list for the spans we are actually cutting."""
    blocks = []
    for n, (s, e) in enumerate(spans, 1):
        txt = ' '.join(sg['text'].strip() for sg in transcript if sg['end'] > s and sg['start'] < e)
        where = ''
        if sections:
            keys = [sm.section_at(sections, t) for t in (s, (s + e) / 2, e - 0.1)]
            labels = [sm.BY_KEY[k]['label'].rstrip('!') for k in dict.fromkeys(keys) if k]
            if labels:
                where = ' - script section: ' + ' then '.join(labels)
        blocks.append(f'--- CLIP {n} ({e - s:.0f}s of call time{where}) ---\n{txt}')
    system = f"""You write Instagram Reels copy for life-insurance agents, and you are also the compliance
screen. The agent is {agent_name}; the other voice is the client. Output strict JSON only.

For each clip give:
  n          - the clip number
  hook       - 6 words or fewer, burned on screen for the first seconds. It must make an agent stop
               scrolling: the tension or the lesson, never a label like "Objection handling". Where a clip
               names its script section, use it: a hook about THE CLOSE sounds like "Prospect tries to
               bounce during the close", not a generic line.
  ig_caption - 2-3 sentences to another agent, then exactly 3 hashtags. No income claims, no carrier names.

Then, across ALL clips, list EVERY word or phrase matching this list:
{PROHIBITED}

JSON: {{"clips":[{{"n":1,"hook":"...","ig_caption":"..."}}],
 "client_names":["<first name>","<last name>","<spouse/family names>"],
 "redactions":[{{"text":"<exact words as they appear above>","type":"health|carrier|location|contact|financial|name"}}]}}

client_names are the client's and their family's names (bleeped on first mention, cut afterwards).
Every other redaction is removed from the audio and the captions. Be exhaustive; missing one is a
compliance failure."""
    j = claude(system, '\n\n'.join(blocks), 6000)
    by_n = {}
    for c in j.get('clips', []):
        if isinstance(c, dict):
            try:
                by_n[int(c.get('n', 0))] = c
            except (TypeError, ValueError):
                continue
    return by_n, [str(n) for n in j.get('client_names', []) if n], j.get('redactions', [])


def compliance_check(caption_text, hook):
    system = """You are a strict compliance reviewer for life-insurance marketing clips (TCPA/HIPAA).
Answer with JSON only: {"flagged": true|false, "unsure": true|false, "items": ["..."]}.
flagged=true if the text contains ANY of the following; unsure=true if you cannot tell.
""" + PROHIBITED
    user = f'Banner: {hook}\n\nCaption text of the clip (**** marks a bleeped name):\n{caption_text}'
    return claude(system, user, 800)


# ---------------------------------------------------------------- camera mode (talking to the audience)
RETAKE_SIM = 0.8                         # how alike two sentences must read to count as takes of one line


def _norm_tokens(text):
    return [t for t in re.sub(r"[^a-z0-9' ]", ' ', text.lower()).split() if t]


def sentence_words(transcript):
    """Sentences as word-lists. Breaks on end punctuation AND on pauses, because a flubbed take is
    usually abandoned mid-sentence and never gets its full stop."""
    ws = [w for sg in transcript for w in sg['words']]
    sents, cur = [], []
    for n, w in enumerate(ws):
        cur.append(w)
        nxt = ws[n + 1] if n + 1 < len(ws) else None
        if re.search(r'[.?!]["\')]?$', str(w['w']).strip()) or nxt is None or nxt['s'] - w['e'] > 0.8:
            sents.append(cur)
            cur = []
    return [s for s in sents if s]


def retake_spans(sents):
    """The cut list that makes 'last take wins' true.

    A retake shows up in a transcript as the same words said again - sometimes finished, sometimes
    abandoned mid-line. Every sentence that reads like an earlier attempt at a LATER one is cut:
    near-identical wording, or a false start that matches the later take's opening."""
    import difflib
    toks = [_norm_tokens(' '.join(str(w['w']) for w in s)) for s in sents]
    cut = [False] * len(sents)
    for i in range(len(sents)):
        ti = toks[i]
        if len(ti) < 3:
            continue
        for j in range(i + 1, len(sents)):
            tj = toks[j]
            if len(tj) < 3:
                continue
            whole = difflib.SequenceMatcher(None, ti, tj).ratio()
            head = difflib.SequenceMatcher(None, ti, tj[:len(ti)]).ratio() if len(tj) > len(ti) else 0.0
            if whole >= RETAKE_SIM or head >= RETAKE_SIM:
                cut[i] = True
                break
    spans = [[s[0]['s'] - 0.05, s[-1]['e'] + 0.05] for s, c in zip(sents, cut) if c]
    return spans, sum(cut)


def camera_cut_plan(transcript, agent_name):
    """Second opinion on the cut: what a human editor would drop that similarity can't see -
    director talk ('okay, take two'), abandoned thoughts, and retakes whose wording changed.
    Also names the finished video."""
    lines = [f"[{sg['start']:.1f}-{sg['end']:.1f}] {sg['text'].strip()}" for sg in transcript]
    system = f"""You are cutting a single-take talking-to-camera video for a life-insurance agent ({agent_name}).
There is NO client; the agent speaks straight to the audience, and may flub lines and re-record them.
Output strict JSON only.

List every stretch that should be REMOVED from the finished video:
  - meta/director talk: "let me start over", "wait", "take two", claps, checking the camera
  - flubbed or abandoned attempts at a line the agent records again afterward. The LAST attempt is
    always the keeper; every earlier attempt goes, even when the wording changed between takes.
Do NOT remove natural speech, pauses or fillers (those are trimmed separately), and never remove
the final take of anything. When in doubt, keep it.

JSON: {{"remove":[{{"start":0,"end":0,"why":"..."}}],"title":"<6 words or fewer: what the video is about>"}}"""
    j = claude(system, 'Transcript with [start-end] seconds per line:\n\n' + '\n'.join(lines), 3000)
    spans = []
    for r in j.get('remove', []):
        try:
            s, e = float(r['start']), float(r['end'])
        except (TypeError, ValueError, KeyError):
            continue
        if e > s:
            spans.append([s, e])
    return spans, str(j.get('title', '') or '').strip()


def process_camera(call, cid, d, video, tell):
    """Talking-to-the-audience mode: one file, no client, no sync. The job is to deliver the take
    the agent MEANT to record - retakes resolved in favor of the last one, dead air and fillers
    trimmed, captions on or off as they chose."""
    vdur = media_duration(video)
    if vdur < 15:
        api('engineProblem', callId=cid, notifyAgent=True,
            reason='Your video is only %s long - there is not enough there to edit. Record your piece and upload again.' % mmss(vdur))
        return
    log.info('%s: camera mode, video %s', cid, mmss(vdur))
    tell(22, 'Listening to your take')
    tpath = os.path.join(d, 'video.json')
    transcript = transcribe(video, tpath, 0.0, vdur)
    words = [w for sg in transcript for w in sg['words']]
    if len(words) < 30:
        api('engineProblem', callId=cid, notifyAgent=True,
            reason='We could not hear enough speech in your video to edit it. Check the camera microphone picked you up clearly, then upload again.')
        return

    tell(45, 'Spotting the retakes')
    cut_spans, n_retakes = retake_spans(sentence_words(transcript))
    title = ''
    try:
        extra, title = camera_cut_plan(transcript, call.get('display_name', 'the agent'))
        # Claude's spans are advisory: clamp to the video, and no single span may swallow the take
        cut_spans += [[max(0.0, s), min(vdur, e)] for s, e in extra if e - s < vdur * 0.6]
    except Exception as ex:
        log.warning('%s: camera cut plan failed (%s); cutting on retakes alone', cid, ex)
    log.info('%s: %d retake cut(s), %d stretch(es) flagged in all', cid, n_retakes, len(cut_spans))

    cap_on = str(call.get('captions', '')).lower() != 'off'
    zoom_want = str(call.get('zoom', '')).lower() in ('1', 'true', 'yes', 'on')
    stamp = when_label(call)
    try:
        dest = drive_mkdir(call_folder_name(call), call['agent_folder_id'])
        log.info('%s: delivering into subfolder "%s"', cid, call_folder_name(call))
    except Exception as ex:
        log.warning('%s: could not create call subfolder (%s); delivering to the main folder', cid, ex)
        dest = call['agent_folder_id']

    tell(65, 'Cutting the flubs and dead air')
    out = os.path.join(d, 'out', 'camera.mp4')
    res = render({'video': video, 'audio': video, 'offset': 0.0, 'transcript': tpath,
                  'start': max(0.0, words[0]['s'] - 0.3), 'end': min(vdur, words[-1]['e'] + 0.5),
                  'hook': '', 'out': out, 'bleep_words': [], 'cut_spans': cut_spans,
                  'gap_max': 0.5, 'gap_pad': 0.15, 'style': call.get('style') or {},
                  'captions': cap_on, 'zoom_cuts': zoom_want})
    tell(90, 'Uploading your clean cut')
    name = (safe_name(title) + ' - ' + stamp) if title else ('Your video (clean cut) - ' + stamp)
    drive_upload(res['out'], name + '.mp4', dest, 'video/mp4')
    api('engineDone', callId=cid, clipFileIds=[], clipCount=0, folderId=dest)
    log.info('%s: camera cut delivered (%.0fs from %.0fs, %d cuts, captions %s%s)', cid,
             res['duration'], vdur, res['cuts'], 'on' if cap_on else 'off',
             ', HDR->709' if res.get('hdr') else '')


# ---------------------------------------------------------------- redaction spans
def _norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


def redaction_spans(transcript, redactions):
    """Find every occurrence of each redaction phrase in the word stream -> [[start,end],...] (audio seconds)."""
    words = [w for sg in transcript for w in sg['words']]
    toks = [_norm(w['w']) for w in words]
    spans = []
    for r in redactions:
        phrase = [t for t in (_norm(x) for x in str(r.get('text', '')).split()) if t]
        if not phrase: continue
        n = len(phrase)
        for i in range(len(toks) - n + 1):
            if toks[i:i + n] == phrase:
                spans.append([words[i]['s'] - 0.02, words[i + n - 1]['e'] + 0.02])
    return spans


# ---------------------------------------------------------------- re-edit jobs (fix a word, cut more clips)
def _replace_words(transcript, span, frm, to):
    """Swap every occurrence of the word/phrase `frm` for `to` inside the span. Returns count."""
    want = [t for t in (_norm(x) for x in str(frm).split()) if t]
    if not want:
        return 0
    n, count = len(want), 0
    for sg in transcript:
        ws = sg['words']
        for i in range(len(ws)):
            if not (span[0] - 2 <= ws[i]['s'] <= span[1] + 2):
                continue
            if [_norm(w['w']) for w in ws[i:i + n]] == want:
                lead = ' ' if str(ws[i]['w']).startswith(' ') else ''
                tail = re.search(r'[.,?!]+$', str(ws[i + n - 1]['w']).strip())
                parts = str(to).split()
                for k in range(n):                # keep timing: spread the new words over the old slots
                    newtxt = parts[k] if k < len(parts) else ''
                    if k == n - 1 and len(parts) > n:
                        newtxt = ' '.join(parts[n - 1:])
                    ws[i + k]['w'] = (lead if k == 0 else ' ') + newtxt if newtxt else ''
                if tail and parts:
                    ws[i + n - 1]['w'] = ws[i + n - 1]['w'].rstrip() + tail.group(0)
                count += 1
    for sg in transcript:                          # drop emptied word slots
        sg['words'] = [w for w in sg['words'] if str(w['w']).strip()]
    return count


def process_job(call, cid, d, job, tell):
    """Re-open a finished call: fix a word in one clip, or cut more clips. Uses the edit state the
    first pass parked beside the clips, so there is no re-transcription. Failures put the call back
    to Ready (engineJobFail) - a bad fix request must never turn a delivered call into a Problem."""
    def fail(reason):
        api('engineJobFail', callId=cid, reason=reason)
        log.info('%s: job failed -> %s', cid, reason)

    try:
        folder = call.get('clip_folder')
        state_id = drive_find('edit_state.json', folder) if folder else None
        if not state_id:
            fail('This call was delivered before re-edits existed, so we cannot re-open it. New uploads will support fixes.')
            return
        tell(10, 'Fetching your call back')
        spath = os.path.join(d, 'edit_state.json')
        drive_download(state_id, spath)
        state = json.load(open(spath))
        video = drive_download(call['video_id'], os.path.join(d, 'video.mp4'))
        audio = drive_download(call['audio_id'], os.path.join(d, 'call.mp3'))
        transcript = state['transcript']
        tpath = os.path.join(d, 'call.json')
        offset, drift, mid_o = state['offset'], state.get('drift', 0.0), state.get('mid_o', 0.0)
        off_at = lambda t: offset + drift * (t - mid_o)   # noqa: E731
        os.makedirs(os.path.join(d, 'out'), exist_ok=True)
        det = state.get('det') or {'anchors': [], 'spans': []}
        all_words = [w for sg in transcript for w in sg['words']]
        app_zone = sm.zone_spans(det['spans'], 'app')

        if job.get('type') == 'fix':
            n = int(job.get('clip', 0))
            rec = next((c for c in state['clips'] if c['i'] == n), None)
            frm, to = str(job.get('from', '')).strip(), str(job.get('to', '')).strip()
            if not rec or not frm or not to:
                fail('That fix request did not match a clip.')
                return
            tell(40, 'Fixing clip %d' % n)
            count = _replace_words(transcript, (rec['start'], rec['end']), frm, to)
            if not count:
                fail('We could not find "%s" in the captions of clip %d, so nothing was changed. '
                     'Check the spelling matches the caption exactly and try again.' % (frm, n))
                return
            json.dump(transcript, open(tpath, 'w'))
            res = render({'video': video, 'audio': audio, 'offset': off_at((rec['start'] + rec['end']) / 2),
                          'cards': sm.plan_cards(det, all_words, rec['start'], rec['end'], bool(state.get('spk'))),
                          'transcript': tpath, 'start': rec['start'], 'end': rec['end'], 'hook': rec['hook'],
                          'out': os.path.join(d, 'out', 'fix.mp4'), 'bleep_words': state['name_keys'],
                          'cut_spans': state['cut_spans'], 'gap_max': 0.5, 'gap_pad': 0.15,
                          'style': state.get('style') or {}, 'zoom_turns': rec.get('zoom_turns'),
                          'name_mode': state.get('name_mode', 'bleep')})
            chk = compliance_check(res['caption_text'], rec['hook'])
            if chk.get('flagged') or chk.get('unsure'):
                fail('That change did not pass the compliance check (%s), so the clip was left as it was.'
                     % ', '.join(map(str, chk.get('items', [])))[:200])
                return
            tell(85, 'Swapping in the fixed clip')
            drive_update(rec['file_id'], res['out'], 'video/mp4')
            if rec.get('thumb_id'):                   # the preview photo keeps up with the fixed captions
                try:
                    tp = os.path.join(d, 'out', 'fixthumb.jpg')
                    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '1', '-i', res['out'],
                                    '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '5', tp], check=True)
                    drive_update(rec['thumb_id'], tp, 'image/jpeg')
                except Exception as ex:
                    log.warning('%s: thumb refresh failed: %s', cid, ex)
            rec['caption_text'] = res['caption_text']
            json.dump(state, open(spath, 'w'))
            drive_update(state_id, spath, 'application/json')
            api('engineDone', callId=cid, clipFileIds=[c['file_id'] for c in state['clips']],
                clipCount=len(state['clips']), folderId=folder, kind='fix', fixedClip=n,
                captions=[c['caption_text'] for c in state['clips']],
                thumbs=[c.get('thumb_id', '') for c in state['clips']],
                igcaps=[c.get('ig_caption', '') for c in state['clips']])
            log.info('%s: fixed clip %d ("%s" -> "%s", %d spot(s))', cid, n, frm, to, count)
            return

        if job.get('type') == 'more':
            json.dump(transcript, open(tpath, 'w'))
            a_min, a_max = state['a_min'], state['a_max']
            taken = [(c['start'], c['end']) for c in state['clips']] + list(app_zone)
            used_names = {c['name'].lower() for c in state['clips']}
            agent_name, stamp = state.get('agent_name', 'the agent'), state.get('stamp', '')
            spk, zoom_want = state.get('spk'), state.get('zoom_want')
            style = state.get('style') or {}
            usable = [sg for sg in transcript if sg['start'] >= a_min and sg['end'] <= a_max]
            tell(30, 'Hunting for more moments')
            moments = [m for m in map_moments(usable, agent_name, spk, det['spans']) if m['end'] > a_min and m['start'] < a_max]
            minute = job.get('minute')
            if minute:
                t0 = max(a_min, float(minute) * 60 - 20)
                moments.append({'kind': 'requested', 'score': 9.9, 'start': t0,
                                'end': min(a_max, t0 + 60), 'title': 'The moment you asked for',
                                'why': 'You asked for the moment around minute %s.' % minute})
            fresh = [m for m in moments
                     if not any(overlap_s(m['start'], m['end'], t0, t1) >
                                0.5 * min(m['end'] - m['start'], t1 - t0) for t0, t1 in taken)]
            chosen = choose_moments(fresh, 3)
            if not chosen:
                fail('We went back through this call but everything worth posting is already in your clips.')
                return
            sents = sentence_bounds(transcript, a_min, a_max)
            rough = []
            for m in chosen:
                fit = fit_span(sents, m['start'], m['end'],
                               lambda s, e: predict_duration(transcript, s, e, (), ()))
                if fit:
                    rough.append((m, fit[0], fit[1]))
            if not rough:
                fail('We found candidate moments but none could be framed into a clip.')
                return
            PAD_SENTS = 3
            screens = []
            for _m, s0, e0 in rough:
                i0 = max(0, min(range(len(sents)), key=lambda k: abs(sents[k][0] - s0)) - PAD_SENTS)
                j0 = min(len(sents) - 1, min(range(len(sents)), key=lambda k: abs(sents[k][1] - e0)) + PAD_SENTS)
                screens.append((sents[i0][0], sents[j0][1], i0, j0))
            copy_by_n, names, redactions = caption_and_redact(usable, [(s, e) for s, e, _, _ in screens], agent_name, det['spans'])
            cut_spans = list(state['cut_spans']) + redaction_spans(transcript, redactions)
            name_keys = sorted(set(state['name_keys']) | {_norm(x) for x in names if len(_norm(x)) >= 3})
            name_mode = state.get('name_mode', 'bleep')
            new_ids = []
            tell(55, 'Cutting your extra clips')
            for n, (m, s0, e0) in enumerate(rough, 1):
                _, _, i0, j0 = screens[n - 1]
                runs = free_runs(sents, i0, j0, taken)
                if not runs:
                    continue
                mid = (s0 + e0) / 2
                run = min(runs, key=lambda r: abs((sents[r[0]][0] + sents[r[-1]][1]) / 2 - mid))
                fit = fit_span([sents[k] for k in run], s0, e0,
                               lambda s, e: predict_duration(transcript, s, e, name_keys, cut_spans,
                                                             name_mode=name_mode))
                if not fit or any(overlap_s(fit[0], fit[1], t0, t1) > 1.0 for t0, t1 in taken):
                    continue
                taken.append((fit[0], fit[1]))
                copy = copy_by_n.get(n, {})
                i = len(state['clips']) + 1
                hook = re.sub(r'[\\/:*?"<>|]', '', str(copy.get('hook') or m.get('title', 'Clip %d' % i)).strip())[:60] or f'Clip {i}'
                out = os.path.join(d, 'out', f'clip{i}.mp4')
                zturns = agent_turns(transcript, fit[0], fit[1]) if (zoom_want and spk) else None
                try:
                    res = render({'video': video, 'audio': audio, 'offset': off_at((fit[0] + fit[1]) / 2),
                                  'cards': sm.plan_cards(det, all_words, fit[0], fit[1], bool(spk)),
                                  'transcript': tpath, 'start': fit[0], 'end': fit[1], 'hook': hook, 'out': out,
                                  'bleep_words': name_keys, 'cut_spans': cut_spans, 'gap_max': 0.5,
                                  'gap_pad': 0.15, 'style': style, 'zoom_turns': zturns, 'name_mode': name_mode})
                except Exception as ex:
                    log.warning('%s: extra clip render failed: %s', cid, ex)
                    continue
                if not CLIP_MIN <= res['duration'] <= CLIP_MAX:
                    continue
                chk = compliance_check(res['caption_text'], hook)
                if chk.get('flagged') or chk.get('unsure'):
                    continue
                name = f'{hook} - {stamp}'
                if name.lower() in used_names:
                    name = f'{hook} ({i}) - {stamp}'
                used_names.add(name.lower())
                try:
                    clip_dest = drive_mkdir(safe_name(hook), folder)
                except Exception:
                    clip_dest = folder
                fid = drive_upload(out, name + '.mp4', clip_dest, 'video/mp4')
                tid = clip_thumb(out, os.path.join(d, 'out'), i, clip_dest)
                caption = str(copy.get('ig_caption', '')).strip()
                try:
                    txt = os.path.join(d, 'out', f'clip{i}.txt')
                    open(txt, 'w').write(caption + '\n')
                    drive_upload(txt, name + '.txt', clip_dest, 'text/plain')
                except Exception:
                    pass
                state['clips'].append({'i': i, 'start': fit[0], 'end': fit[1], 'hook': hook, 'name': name,
                                       'file_id': fid, 'folder_id': clip_dest, 'thumb_id': tid,
                                       'caption_text': res['caption_text'], 'zoom_turns': zturns,
                                       'ig_caption': caption})
                new_ids.append(fid)
                log.info('%s: extra clip delivered "%s" (%.0fs)', cid, hook, res['duration'])
            if not new_ids:
                fail('We went back through this call but nothing new passed the quality and compliance bar.')
                return
            json.dump(state, open(spath, 'w'))
            drive_update(state_id, spath, 'application/json')
            api('engineDone', callId=cid, clipFileIds=[c['file_id'] for c in state['clips']],
                clipCount=len(state['clips']), folderId=folder, kind='more', newCount=len(new_ids),
                captions=[c['caption_text'] for c in state['clips']],
                thumbs=[c.get('thumb_id', '') for c in state['clips']],
                igcaps=[c.get('ig_caption', '') for c in state['clips']])
            log.info('%s: done, %d extra clip(s) delivered', cid, len(new_ids))
            return

        fail('Unknown job type.')
    except Exception as ex:
        log.exception('%s: job failed', cid)
        try:
            api('engineJobFail', callId=cid, reason=f'Engine error during the re-edit: {ex}')
        except Exception:
            pass


# ---------------------------------------------------------------- one call
def process(call):
    cid = call['call_id']
    d = os.path.join(WORK, cid)
    shutil.rmtree(d, ignore_errors=True); os.makedirs(os.path.join(d, 'out'))
    try:
        def tell(pct, label):
            """Stage report for the Editor Island progress bar; never worth failing a call over."""
            try:
                api('engineProgress', callId=cid, pct=pct, label=label)
            except Exception:
                pass

        job = {}
        try:
            job = json.loads(call.get('job')) if call.get('job') else {}
        except Exception:
            job = {}
        if job.get('type'):                      # a re-edit of a finished call, not a fresh upload
            process_job(call, cid, d, job, tell)
            return

        log.info('%s: downloading', cid)
        tell(6, 'Flying your files to the island')
        video = drive_download(call['video_id'], os.path.join(d, 'video.mp4'))
        mode = call.get('mode') or 'clips'
        if mode == 'camera':                     # talking to the audience: one file, nothing to sync
            process_camera(call, cid, d, video, tell)
            return
        audio = drive_download(call['audio_id'], os.path.join(d, 'call.mp3'))

        log.info('%s: syncing', cid)
        tell(18, 'Lining the call audio up with your video')
        offset, conf, detail, drift, win = sync_offset(video, audio, d)
        vdur = media_duration(video)
        adur = media_duration(audio)
        log.info('%s: offset %.3f confidence %.1f (%s) | video %s, recording %s',
                 cid, offset, conf, detail, mmss(vdur), mmss(adur))
        if conf < MIN_SYNC_CONFIDENCE:
            api('engineProblem', callId=cid, notifyAgent=True,
                reason=sync_problem(detail, offset, vdur, adur))
            return

        # RULE: edit only where there is video. Audio t = video t - offset, so the usable call window is:
        a_min = max(0.0, -offset) + 0.5
        a_max = min(adur, vdur - offset) - 0.5
        # a drift fit may also confine us to the stretch of the call it could actually verify
        if win:
            a_min = max(a_min, win[0] + 0.5)
            a_max = min(a_max, win[1] - 0.5)
        # on a drifting call the offset is only exact at the middle; keep the edges clear of the
        # worst-case misalignment, and give every later cut its own drift-corrected offset
        if drift:
            pad = abs(drift) * (a_max - a_min) / 2 + 0.5
            a_min += pad
            a_max -= pad
        mid_o = (win[0] + win[1]) / 2.0 if win else (a_min + a_max) / 2.0
        off_at = lambda t: offset + drift * (t - mid_o)   # noqa: E731
        log.info('%s: video %.0fs, recording %.0fs, usable window %.1f-%.1f s (%.0fs)', cid, vdur, adur, a_min, a_max, a_max - a_min)
        # a sync-only job just needs enough shared time to line up; clips need room to cut from
        if a_max - a_min < (10 if mode == 'sync' else 20):
            api('engineProblem', callId=cid, notifyAgent=True,
                reason=('The two files lined up, but only %s of the call was actually on camera - the '
                        'minimum we can %s is 20 seconds.\nYour video is %s long and your call recording '
                        'is %s.\nFilm from the start of the call, and check your dialer captured the whole '
                        'thing, then upload again.'
                        % (mmss(max(0.0, a_max - a_min)),
                           'work with' if mode == 'sync' else 'cut a clip from',
                           mmss(vdur), mmss(adur))))
            return
        # short videos still deserve a clip: on anything under a minute, the length floor
        # relaxes to what the video can actually give (never below 8s)
        lo_min = CLIP_MIN if a_max - a_min >= 60 else max(8.0, min(CLIP_MIN, (a_max - a_min) * 0.6))

        style = call.get('style') or {}
        stamp = when_label(call)                      # '2026-10-26 - 6.42 PM ET', shared by every name here
        delivered, dropped, used_names, state_clips = [], [], set(), []

        # each upload gets its OWN subfolder inside the agent's folder, so two calls never pile into one place
        try:
            dest = drive_mkdir(call_folder_name(call), call['agent_folder_id'])
            log.info('%s: delivering into subfolder "%s"', cid, call_folder_name(call))
        except Exception as ex:
            log.warning('%s: could not create call subfolder (%s); delivering to the main folder', cid, ex)
            dest = call['agent_folder_id']

        # always deliver the full synced video (original quality, clean call audio) so agents can edit themselves
        try:
            full = synced_full_video(video, audio, off_at((a_min + a_max) / 2), a_min, a_max,
                                     os.path.join(d, 'out', 'full.mp4'), drift)
            fid_full = drive_upload(full, f'Full call (synced audio) - {stamp}.mp4', dest, 'video/mp4')
            log.info('%s: delivered full synced video (%.0fs)', cid, a_max - a_min)
            tell(30, 'Full synced video delivered')
        except Exception as ex:
            log.warning('%s: full synced video failed: %s', cid, ex); fid_full = None
        if mode == 'sync':
            if fid_full:
                sync_tips = (['Part of your call recording did not line up with the video (your dialer may '
                              'have paused while recording), so the synced video covers the clean stretch '
                              'only.'] if win else [])
                api('engineDone', callId=cid, clipFileIds=[], clipCount=0, folderId=dest, notes=sync_tips)
            else:
                api('engineProblem', callId=cid, reason='Could not build the synced full video.')
            return

        log.info('%s: transcribing the video window only', cid)
        tell(35, 'Listening to the whole call')
        tpath = os.path.join(d, 'call.json')
        transcript = transcribe(audio, tpath, a_min, a_max)
        usable = [sg for sg in transcript if sg['start'] >= a_min and sg['end'] <= a_max]
        if sum(len(sg['words']) for sg in usable) < 80:
            api('engineProblem', callId=cid, notifyAgent=True, reason='The part of the call that has video was too short or too quiet to transcribe.')
            return

        agent_name = call.get('display_name', 'the agent')
        # who is speaking, from the phone video's own mic against the dialer recording
        try:
            spk, spk_conf, spk_detail = speaker_labels(os.path.join(d, 'v.wav'), os.path.join(d, 'a.wav'),
                                                       offset, [w for sg in transcript for w in sg['words']])
        except Exception as ex:                       # never fail a call over the speaker split
            spk, spk_conf, spk_detail = False, 0.0, 'speaker check failed (%s)' % ex
            log.warning('%s: speaker detection error: %s', cid, ex)
        log.info('%s: speakers: %s', cid, spk_detail if spk else 'not identified (%s)' % spk_detail)

        # where in the sales script the agent is, line by line: context for the picker and the hooks,
        # the on-screen live-call cards, and a hard no-clip zone around the application
        try:
            det = sm.detect(transcript)
        except Exception as ex:
            log.warning('%s: section detection failed: %s', cid, ex); det = {'anchors': [], 'spans': []}
        app_zone = sm.zone_spans(det['spans'], 'app')
        log.info('%s: script sections: %s', cid, ' > '.join('%s@%s' % (sp['key'], mmss(sp['start'])) for sp in det['spans']) or 'none recognised')
        all_words = [w for sg in transcript for w in sg['words']]

        # zoom mode is per upload; it needs to know who is talking, so it rides on the speaker labels
        zoom_want = str(call.get('zoom', '')).lower() in ('1', 'true', 'yes', 'on')
        name_mode = 'cut' if str(call.get('name_mode', '')).lower() == 'cut' else 'bleep'
        zoom_note = ''
        if zoom_want and not spk:
            zoom_note = ("Zoom was switched on for this call, but we could not reliably tell your voice from "
                         "the client's on the phone video, so these clips are flat. Recording on headphones - "
                         "so the client is not coming back out of your phone speaker - is what lets it work.")
            log.info('%s: zoom requested but speakers are not separable; delivering flat', cid)

        # footage coaching: one or two tips in the delivery email when the engine noticed something
        # the agent can fix next time. Only things they can act on - never scolding.
        tips = []
        if zoom_want and not spk:
            tips.append('Wear headphones next time - when the client is not coming out of your phone '
                        'speaker, we can tell your voices apart and the zoom effect works.')
        if win:
            tips.append('Part of your call recording did not line up with the video (your dialer may have '
                        'paused or skipped while recording), so we edited from the clean stretch only. If '
                        'this keeps happening, check your dialer\'s recording settings.')
        if a_max - a_min < min(adur, vdur) - 120:
            tips.append('A big chunk of your files had no overlap - start the camera and the recording at '
                        'roughly the same time and more of the call makes it in.')

        section_words = str(call.get('section') or '').strip()
        if mode == 'section' and section_words:
            # the agent quoted the exact part they want: find those words and build the one clip around them
            log.info('%s: looking for the requested section ("%s")', cid, section_words[:60])
            span = find_phrase(transcript, section_words, a_min, a_max)
            if not span:
                api('engineProblem', callId=cid,
                    reason='We could not find the words "%s" in the part of the call that has video. '
                           'Check they match what was said on camera (a shorter, distinctive phrase works best) '
                           'and upload again.' % section_words[:120])
                return
            log.info('%s: section found at %.0f-%.0fs', cid, span[0], span[1])
            chosen = [{'kind': 'section', 'score': 9.9, 'title': 'The part you asked for',
                       'start': span[0], 'end': min(span[1] + 20, a_max),
                       'why': 'You asked for this exact part of the call.'}]
        else:
            log.info('%s: mapping the call', cid)
            moments = [m for m in map_moments(usable, agent_name, spk, det['spans']) if m['end'] > a_min and m['start'] < a_max]
            moments = [m for m in moments if not any(overlap_s(m['start'], m['end'], z0, z1) > 0.5 for z0, z1 in app_zone)]
            chosen = choose_moments(moments, 5)
            log.info('%s: %d moments found, %d chosen (%s)', cid, len(moments), len(chosen),
                     ', '.join('%s %.0f' % (m['kind'], m['score']) for m in chosen) or 'none')
        if not chosen and (a_max - a_min) <= 95:
            log.info('%s: nothing mapped on a short video - the whole usable stretch becomes the clip', cid)
            chosen = [{'kind': 'highlight', 'score': 5.0, 'title': 'Your video',
                       'start': a_min, 'end': a_max,
                       'why': 'Short video: the whole usable stretch, cleaned up.'}]
        if not chosen:
            if fid_full:
                api('engineDone', callId=cid, clipFileIds=[], clipCount=0, folderId=dest)
                log.info('%s: no teachable moment found; full synced video delivered', cid)
            else:
                api('engineProblem', callId=cid, reason='No teachable moment was found in the part of the call that has video.')
            return

        # size each moment by what it will BE after cutting, not by how the transcript reads
        sents = sentence_bounds(transcript, a_min, a_max)
        rough = []
        for m in chosen:
            fit = fit_span(sents, m['start'], m['end'],
                           lambda s, e: predict_duration(transcript, s, e, (), ()), lo=lo_min)
            if fit:
                rough.append((m, fit[0], fit[1]))
                log.info('%s: "%s" %.0f-%.0f -> %.0f-%.0f (~%.0fs)', cid, m.get('title', '')[:40],
                         m['start'], m['end'], fit[0], fit[1], fit[2])
            else:
                dropped.append('%s: could not be framed into a clip' % str(m.get('title', 'moment'))[:40])
        if not rough:
            api('engineProblem', callId=cid, reason='Nothing could be framed into a clip.\n' + '\n'.join(dropped))
            return

        # pass 2 screens a wider window than each clip, so a later re-fit can still grow into vetted text
        PAD_SENTS = 3
        screens = []
        for _m, s0, e0 in rough:
            i0 = max(0, min(range(len(sents)), key=lambda k: abs(sents[k][0] - s0)) - PAD_SENTS)
            j0 = min(len(sents) - 1, min(range(len(sents)), key=lambda k: abs(sents[k][1] - e0)) + PAD_SENTS)
            screens.append((sents[i0][0], sents[j0][1], i0, j0))
        log.info('%s: writing hooks, captions and redactions', cid)
        copy_by_n, names, redactions = caption_and_redact(usable, [(s, e) for s, e, _, _ in screens], agent_name, det['spans'])
        cut_spans = redaction_spans(transcript, redactions)
        name_keys = sorted({_norm(n) for n in names if len(_norm(n)) >= 3})
        log.info('%s: %d name keys, %d cut spans', cid, len(name_keys), len(cut_spans))
        tell(55, 'Picking the best moments')

        # re-fit now that the real cuts are known, staying inside the screened sentences - and never
        # inside footage an earlier clip already claimed. rough is best-first, so when two moments
        # converge on the same stretch the stronger one keeps it and the weaker is dropped, not shipped
        # as a near-identical twin.
        clips, taken = [], list(app_zone)       # the application is footage no clip may claim
        for n, (m, s0, e0) in enumerate(rough, 1):
            _, _, i0, j0 = screens[n - 1]
            title = str(m.get('title', 'moment'))[:40]
            runs = free_runs(sents, i0, j0, taken)
            if not runs:
                dropped.append('%s: covers the same footage as an earlier clip' % title)
                continue
            mid = (s0 + e0) / 2
            run = min(runs, key=lambda r: abs((sents[r[0]][0] + sents[r[-1]][1]) / 2 - mid))
            fit = fit_span([sents[k] for k in run], s0, e0,
                           lambda s, e: predict_duration(transcript, s, e, name_keys, cut_spans,
                                                         name_mode=name_mode), lo=lo_min)
            if not fit:
                dropped.append('%s: nothing left after compliance cuts' % title)
                continue
            if any(overlap_s(fit[0], fit[1], t0, t1) > 1.0 for t0, t1 in taken):
                dropped.append('%s: overlaps an earlier clip' % title)
                continue
            taken.append((fit[0], fit[1]))
            copy = copy_by_n.get(n, {})
            clips.append({'start': fit[0], 'end': fit[1], 'rank': len(clips) + 1, 'kind': m.get('kind', ''),
                          'hook': copy.get('hook') or m.get('title', 'Clip %d' % n),
                          'ig_caption': copy.get('ig_caption', ''), 'why': m.get('why', ''),
                          'score': m.get('score', 0)})

        for i, c in enumerate(clips, 1):
            hook = re.sub(r'[\\/:*?"<>|]', '', str(c.get('hook', f'Clip {i}')).strip())[:60] or f'Clip {i}'
            tell(min(97, 57 + int(40 * (i - 1) / max(1, len(clips)))), f'Cutting clip {i} of {len(clips)}')
            out = os.path.join(d, 'out', f'clip{i}.mp4')
            zturns = agent_turns(transcript, float(c['start']), float(c['end'])) if (zoom_want and spk) else None
            cards = sm.plan_cards(det, all_words, float(c['start']), float(c['end']), bool(spk))
            try:
                res = render({'video': video, 'audio': audio, 'cards': cards,
                              'offset': (local_refine(off_at((float(c['start']) + float(c['end'])) / 2), float(c['start']), float(c['end']))
                                         if win else off_at((float(c['start']) + float(c['end'])) / 2)), 'transcript': tpath,
                              'start': float(c['start']), 'end': float(c['end']), 'hook': hook, 'out': out,
                              'bleep_words': name_keys, 'cut_spans': cut_spans, 'gap_max': 0.5, 'gap_pad': 0.15,
                              'style': style, 'zoom_turns': zturns, 'name_mode': name_mode})
            except Exception as ex:
                log.warning('%s: clip %d render failed: %s', cid, i, ex); dropped.append(f'{hook}: render failed'); continue
            if not max(6.0, lo_min - 4) <= res['duration'] <= CLIP_MAX:
                dropped.append(f'{hook}: {res["duration"]:.0f}s after trimming'); continue
            chk = compliance_check(res['caption_text'], hook)
            if chk.get('flagged') or chk.get('unsure'):
                dropped.append(f'{hook}: compliance ({", ".join(map(str, chk.get("items", [])))[:200]})'); continue
            # name: the clip's own hook, then the date and Eastern time of the upload
            name = f'{hook} - {stamp}'
            folder_title = safe_name(hook)                         # the clip's folder is just its title
            if name.lower() in used_names:                         # two clips with the same hook
                name = f'{hook} ({i}) - {stamp}'
                folder_title = safe_name(f'{hook} ({i})')
            used_names.add(name.lower())
            # each clip gets its own subfolder inside the call folder: one video + one clip sheet
            try:
                clip_dest = drive_mkdir(folder_title, dest)
            except Exception as ex:
                log.warning('%s: could not create clip folder "%s" (%s); delivering to the call folder',
                            cid, folder_title, ex)
                clip_dest = dest
            fid = drive_upload(out, name + '.mp4', clip_dest, 'video/mp4')
            tid = clip_thumb(out, os.path.join(d, 'out'), i, clip_dest)

            # clip sheet: thumbnail, copy-and-paste caption, and why it was cut this way
            caption = str(c.get('ig_caption', '')).strip()
            try:
                if caption_pdf is None:
                    raise RuntimeError('reportlab not installed')
                pdf = caption_pdf.build({
                    'out': os.path.join(d, 'out', f'clip{i}.pdf'), 'clip': out, 'hook': hook,
                    'caption': caption, 'why': str(c.get('why', '')).strip(),
                    'agent': call.get('display_name', ''), 'when': stamp, 'duration': res['duration'],
                    'rank': c.get('rank'), 'kind': c.get('kind'), 'of': len(clips), 'zoom_note': zoom_note,
                    'edits': {k: res.get(k) for k in ('picked', 'fillers', 'tightened', 'bleeps',
                                                      'redacted', 'kept_pauses', 'zooms')}})
                drive_upload(pdf, name + '.pdf', clip_dest, 'application/pdf')
            except Exception as ex:                                # never lose the caption over a PDF problem
                log.warning('%s: clip sheet failed (%s); delivering the caption as text', cid, ex)
                txt = os.path.join(d, 'out', f'clip{i}.txt')
                open(txt, 'w').write(caption + '\n')
                drive_upload(txt, name + '.txt', clip_dest, 'text/plain')
            delivered.append(fid)
            state_clips.append({'i': len(delivered), 'start': float(c['start']), 'end': float(c['end']),
                                'hook': hook, 'name': name, 'file_id': fid, 'folder_id': clip_dest,
                                'thumb_id': tid, 'caption_text': res['caption_text'], 'zoom_turns': zturns,
                                'ig_caption': str(c.get('ig_caption', '')).strip(),
                                'section': (sm.BY_KEY[sm.section_at(det['spans'], float(c['start']))]['label']
                                            if sm.section_at(det['spans'], float(c['start'])) else '')})
            log.info('%s: delivered "%s" (%.0fs, %d bleeps%s; cards: %s)', cid, hook, res['duration'], res['bleeps'],
                     ', HDR->709' if res.get('hdr') else '', ', '.join(t for _, _, t in cards) or 'none')

        for dmsg in dropped:
            log.info('%s: dropped -> %s', cid, dmsg)
        if delivered:
            # everything a later fix-a-word or cut-more-clips job needs, parked beside the clips
            try:
                state = {'call_id': cid, 'offset': offset, 'drift': drift, 'mid_o': mid_o,
                         'a_min': a_min, 'a_max': a_max, 'spk': bool(spk), 'zoom_want': bool(zoom_want),
                         'style': style, 'name_keys': list(name_keys), 'cut_spans': cut_spans,
                         'name_mode': name_mode, 'stamp': stamp, 'agent_name': agent_name,
                         'transcript': transcript, 'clips': state_clips, 'det': det}
                spath = os.path.join(d, 'edit_state.json')
                json.dump(state, open(spath, 'w'))
                drive_upload(spath, 'edit_state.json', dest, 'application/json')
            except Exception as ex:                   # jobs just won't be available for this call
                log.warning('%s: could not park edit state: %s', cid, ex)
            api('engineDone', callId=cid, clipFileIds=delivered, clipCount=len(delivered), folderId=dest,
                captions=[sc['caption_text'] for sc in state_clips], notes=tips,
                thumbs=[sc.get('thumb_id', '') for sc in state_clips],
                igcaps=[sc.get('ig_caption', '') for sc in state_clips],
                sections=[sc.get('section', '') for sc in state_clips])
            log.info('%s: done, %d delivered, %d dropped', cid, len(delivered), len(dropped))
        elif fid_full:
            api('engineDone', callId=cid, clipFileIds=[], clipCount=0, folderId=dest, notes=tips)
            log.info('%s: no clip passed; full synced video delivered', cid)
        else:
            api('engineProblem', callId=cid, reason='No clip passed the compliance check.\n' + '\n'.join(dropped))
    except Exception as ex:
        log.exception('%s: failed', cid)
        try: api('engineProblem', callId=cid, reason=f'Engine error: {ex}')
        except Exception: pass
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    os.makedirs(WORK, exist_ok=True)
    log.info('engine up: model=%s whisper=%s poll=%ss', MODEL, WHISPER, POLL)
    while True:
        try:
            nxt = api('engineNext').get('call')
            if nxt:
                process(nxt)
                continue   # look for more right away
        except Exception as ex:
            log.error('poll failed: %s', ex)
        time.sleep(POLL)


if __name__ == '__main__':
    main()
