#!/usr/bin/env python3
"""My Dear Academy editing engine.
Polls the Apps Script doorman, downloads each uploaded call from Mason's Drive, syncs the dialer audio to the
phone video, transcribes, asks Claude for clips + redactions, renders Reels, re-checks compliance, uploads the
survivors to the agent's folder and reports back. No Google login lives here: Drive access is a short-lived
token handed out by the doorman.

Env (see /opt/mda/.env): MDA_API_URL, MDA_ENGINE_SECRET, ANTHROPIC_API_KEY,
optional: CLAUDE_MODEL, WHISPER_MODEL, MDA_WORK_DIR, MDA_POLL_SECONDS
"""
import os, sys, json, time, re, shutil, subprocess, logging, datetime
from zoneinfo import ZoneInfo
import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from render import render, predict_duration  # noqa: E402
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


def synced_full_video(video, audio, offset, a_min, a_max, out):
    """Original phone video (no re-encode) with the clean call audio replacing the phone audio, trimmed to the overlap."""
    v0 = a_min + offset
    dur = a_max - a_min
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{v0:.3f}', '-t', f'{dur:.3f}', '-i', video,
                    '-ss', f'{a_min:.3f}', '-t', f'{dur:.3f}', '-i', audio,
                    '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', out], check=True)
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
    c = s.correlate(ev, ea, mode='full', method='fft')
    k = int(np.argmax(c))
    lag = k - (len(ea) - 1)
    peak_z = float((c[k] - np.median(c)) / (np.std(c) + 1e-9))

    adur, vdur = len(a) / 8000.0, len(v) / 8000.0
    EPS = 8000 // HOP                             # envelope points per second (50)

    def halves_agree_at(off_s):
        """Validate a candidate offset using ONLY the stretch of the recording that overlaps the video:
        both halves of that overlap must independently land on (nearly) the same offset. This also
        works when the video is just a short snippet of a much longer call."""
        o0, o1 = max(0.0, -off_s), min(adur, vdur - off_s)
        if o1 - o0 < 40:                          # too little shared time to judge (also rejected later)
            return None
        mid = (o0 + o1) / 2.0
        got = []
        for s0, s1 in ((o0, mid), (mid, o1)):
            i0 = int(s0 * EPS)
            seg = ea[i0:int(s1 * EPS)]
            if len(seg) < 15 * EPS:
                return None
            seg = seg - seg.mean()
            cs = s.correlate(ev, seg, mode='full', method='fft')
            got.append(((int(np.argmax(cs)) - (len(seg) - 1)) - i0) / float(EPS))
        return (got[0] + got[1]) / 2.0 if abs(got[0] - got[1]) <= 0.35 else None

    # candidate offsets: the global peak, plus what each half of the whole recording suggests on its own
    # (with a snippet video the global peak can land in the wrong place; one of these usually has it)
    half = len(ea) // 2
    cands = [lag / float(EPS)]
    for h0, seg in ((0, ea[:half]), (half, ea[half:])):
        seg = seg - seg.mean()
        if len(seg) >= 1500:
            cs = s.correlate(ev, seg, mode='full', method='fft')
            cands.append(((int(np.argmax(cs)) - (len(seg) - 1)) - h0) / float(EPS))
    off, agree, tried = cands[0], False, []
    for cand in cands:
        if any(abs(cand - t) < 1.0 for t in tried):
            continue
        tried.append(cand)
        got = halves_agree_at(cand)
        if got is not None:
            off, agree = got, True
            break
    detail = (f'overlap halves agree at {off:+.1f}s' if agree else
              'no overlap agreement (candidates ' + ', '.join(f'{t:+.1f}s' for t in tried) + ')')
    # sample-accurate refine on a 20 s window in the middle of the overlap
    adur, vdur = len(a) / 8000.0, len(v) / 8000.0
    o0, o1 = max(0.0, -off), min(adur, vdur - off)
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
    return float(off), conf, f'peak {peak_z:.1f}, {detail}'


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

    if adur < 180 or adur < vdur * 0.4:
        out.append('The recording is much shorter than the video. That is usually one of two things: your '
                   'dialer only caught part of the call, or the camera was rolling for a long time before '
                   'the call began. Anything filmed before the call starts cannot be matched or used, so '
                   'start recording when you dial.')
    elif vdur < adur * 0.4:
        out.append('The video is much shorter than the recording. If you only filmed part of the call, '
                   'there may not be enough footage to work with.')
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


def map_moments(transcript, agent_name, labelled=False):
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


def choose_moments(moments, limit=5):
    """Best first - but not five versions of the same lesson. Every kind gets a slot before any repeats.

    Variety never reaches below the quality floor: a weak moment of a missing kind is worse than a second
    strong one of a kind already covered."""
    ranked = sorted([m for m in moments if m['score'] >= KIND_FLOOR.get(m['kind'], MIN_SCORE)],
                    key=lambda m: -m['score'])
    picked, kinds = [], set()
    for m in ranked:
        if len(picked) >= limit:
            break
        if m['kind'] not in kinds:
            picked.append(m)
            kinds.add(m['kind'])
    for m in ranked:
        if len(picked) >= limit:
            break
        if not any(m is p for p in picked):
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


def caption_and_redact(transcript, spans, agent_name):
    """Pass 2 - banner, caption and the exhaustive redaction list for the spans we are actually cutting."""
    blocks = []
    for n, (s, e) in enumerate(spans, 1):
        txt = ' '.join(sg['text'].strip() for sg in transcript if sg['end'] > s and sg['start'] < e)
        blocks.append(f'--- CLIP {n} ({e - s:.0f}s of call time) ---\n{txt}')
    system = f"""You write Instagram Reels copy for life-insurance agents, and you are also the compliance
screen. The agent is {agent_name}; the other voice is the client. Output strict JSON only.

For each clip give:
  n          - the clip number
  hook       - 6 words or fewer, burned on screen for the first seconds. It must make an agent stop
               scrolling: the tension or the lesson, never a label like "Objection handling".
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


# ---------------------------------------------------------------- one call
def process(call):
    cid = call['call_id']
    d = os.path.join(WORK, cid)
    shutil.rmtree(d, ignore_errors=True); os.makedirs(os.path.join(d, 'out'))
    try:
        log.info('%s: downloading', cid)
        video = drive_download(call['video_id'], os.path.join(d, 'video.mp4'))
        audio = drive_download(call['audio_id'], os.path.join(d, 'call.mp3'))

        log.info('%s: syncing', cid)
        offset, conf, detail = sync_offset(video, audio, d)
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
        log.info('%s: video %.0fs, recording %.0fs, usable window %.1f-%.1f s (%.0fs)', cid, vdur, adur, a_min, a_max, a_max - a_min)
        if a_max - a_min < 45:
            api('engineProblem', callId=cid, notifyAgent=True,
                reason=('The two files lined up, but only %s of the call was actually on camera, which is '
                        'not enough to cut anything from.\nYour video is %s long and your call recording '
                        'is %s.\nFilm from the start of the call, and check your dialer captured the whole '
                        'thing, then upload again.'
                        % (mmss(max(0.0, a_max - a_min)), mmss(vdur), mmss(adur))))
            return

        mode = call.get('mode') or 'clips'
        style = call.get('style') or {}
        stamp = when_label(call)                      # '2026-10-26 - 6.42 PM ET', shared by every name here
        delivered, dropped, used_names = [], [], set()

        # each upload gets its OWN subfolder inside the agent's folder, so two calls never pile into one place
        try:
            dest = drive_mkdir(call_folder_name(call), call['agent_folder_id'])
            log.info('%s: delivering into subfolder "%s"', cid, call_folder_name(call))
        except Exception as ex:
            log.warning('%s: could not create call subfolder (%s); delivering to the main folder', cid, ex)
            dest = call['agent_folder_id']

        # always deliver the full synced video (original quality, clean call audio) so agents can edit themselves
        try:
            full = synced_full_video(video, audio, offset, a_min, a_max, os.path.join(d, 'out', 'full.mp4'))
            fid_full = drive_upload(full, f'Full call (synced audio) - {stamp}.mp4', dest, 'video/mp4')
            log.info('%s: delivered full synced video (%.0fs)', cid, a_max - a_min)
        except Exception as ex:
            log.warning('%s: full synced video failed: %s', cid, ex); fid_full = None
        if mode == 'sync':
            if fid_full:
                api('engineDone', callId=cid, clipFileIds=[], clipCount=0, folderId=dest)
            else:
                api('engineProblem', callId=cid, reason='Could not build the synced full video.')
            return

        log.info('%s: transcribing the video window only', cid)
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

        # zoom mode is per upload; it needs to know who is talking, so it rides on the speaker labels
        zoom_want = str(call.get('zoom', '')).lower() in ('1', 'true', 'yes', 'on')
        name_mode = 'cut' if str(call.get('name_mode', '')).lower() == 'cut' else 'bleep'
        zoom_note = ''
        if zoom_want and not spk:
            zoom_note = ("Zoom was switched on for this call, but we could not reliably tell your voice from "
                         "the client's on the phone video, so these clips are flat. Recording on headphones - "
                         "so the client is not coming back out of your phone speaker - is what lets it work.")
            log.info('%s: zoom requested but speakers are not separable; delivering flat', cid)

        log.info('%s: mapping the call', cid)
        moments = [m for m in map_moments(usable, agent_name, spk) if m['end'] > a_min and m['start'] < a_max]
        chosen = choose_moments(moments, 5)
        log.info('%s: %d moments found, %d chosen (%s)', cid, len(moments), len(chosen),
                 ', '.join('%s %.0f' % (m['kind'], m['score']) for m in chosen) or 'none')
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
                           lambda s, e: predict_duration(transcript, s, e, (), ()))
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
        copy_by_n, names, redactions = caption_and_redact(usable, [(s, e) for s, e, _, _ in screens], agent_name)
        cut_spans = redaction_spans(transcript, redactions)
        name_keys = sorted({_norm(n) for n in names if len(_norm(n)) >= 3})
        log.info('%s: %d name keys, %d cut spans', cid, len(name_keys), len(cut_spans))

        # re-fit now that the real cuts are known, staying inside the screened sentences
        clips = []
        for n, (m, s0, e0) in enumerate(rough, 1):
            _, _, i0, j0 = screens[n - 1]
            fit = fit_span(sents[i0:j0 + 1], s0, e0,
                           lambda s, e: predict_duration(transcript, s, e, name_keys, cut_spans,
                                                         name_mode=name_mode))
            if not fit:
                dropped.append('%s: nothing left after compliance cuts' % str(m.get('title', 'moment'))[:40])
                continue
            copy = copy_by_n.get(n, {})
            clips.append({'start': fit[0], 'end': fit[1], 'rank': len(clips) + 1, 'kind': m.get('kind', ''),
                          'hook': copy.get('hook') or m.get('title', 'Clip %d' % n),
                          'ig_caption': copy.get('ig_caption', ''), 'why': m.get('why', ''),
                          'score': m.get('score', 0)})

        for i, c in enumerate(clips, 1):
            hook = re.sub(r'[\\/:*?"<>|]', '', str(c.get('hook', f'Clip {i}')).strip())[:60] or f'Clip {i}'
            out = os.path.join(d, 'out', f'clip{i}.mp4')
            zturns = agent_turns(transcript, float(c['start']), float(c['end'])) if (zoom_want and spk) else None
            try:
                res = render({'video': video, 'audio': audio, 'offset': offset, 'transcript': tpath,
                              'start': float(c['start']), 'end': float(c['end']), 'hook': hook, 'out': out,
                              'bleep_words': name_keys, 'cut_spans': cut_spans, 'gap_max': 0.5, 'gap_pad': 0.15,
                              'style': style, 'zoom_turns': zturns, 'name_mode': name_mode})
            except Exception as ex:
                log.warning('%s: clip %d render failed: %s', cid, i, ex); dropped.append(f'{hook}: render failed'); continue
            if not CLIP_MIN <= res['duration'] <= CLIP_MAX:
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
            log.info('%s: delivered "%s" (%.0fs, %d bleeps)', cid, hook, res['duration'], res['bleeps'])

        for dmsg in dropped:
            log.info('%s: dropped -> %s', cid, dmsg)
        if delivered:
            api('engineDone', callId=cid, clipFileIds=delivered, clipCount=len(delivered), folderId=dest)
            log.info('%s: done, %d delivered, %d dropped', cid, len(delivered), len(dropped))
        elif fid_full:
            api('engineDone', callId=cid, clipFileIds=[], clipCount=0, folderId=dest)
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
