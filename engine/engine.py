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
import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from render import render  # noqa: E402

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


# ---------------------------------------------------------------- media
def wav8k(src, dst):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-ac', '1', '-ar', '8000', '-vn', dst], check=True)
    import scipy.io.wavfile as w
    _, x = w.read(dst)
    return x.astype(np.float32)


def media_duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout.strip()
    try: return float(out)
    except ValueError: return 0.0


def sync_offset(video, audio, tmp):
    """Returns (offset_seconds, confidence). Audio t=0 sits at video t=offset."""
    import scipy.signal as s
    v = wav8k(video, os.path.join(tmp, 'v.wav'))
    a = wav8k(audio, os.path.join(tmp, 'a.wav'))
    # correlate on at most the first 6 minutes of each to keep memory sane; offsets rarely exceed that
    N = 8000 * 360
    v, a = v[:N], a[:N]
    c = s.correlate(v - v.mean(), a - a.mean(), mode='full', method='fft')
    offset = (np.argmax(c) - (len(a) - 1)) / 8000
    conf = float(c.max() / (np.std(c) + 1e-9))
    return float(offset), conf


def transcribe(audio, out_json):
    from faster_whisper import WhisperModel
    m = WhisperModel(WHISPER, device='cpu', compute_type='int8')
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', audio, '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'],
                         capture_output=True, check=True).stdout
    arr = np.frombuffer(raw, np.float32)
    segs, _ = m.transcribe(arr, language='en', word_timestamps=True, vad_filter=False, beam_size=5)
    out = []
    for sg in segs:
        if not sg.words: continue
        out.append({'start': sg.start, 'end': sg.end, 'text': sg.text,
                    'words': [{'w': w.word, 's': w.start, 'e': w.end} for w in sg.words]})
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


def pick_clips(transcript, agent_name):
    lines = [f"[{sg['start']:.1f}-{sg['end']:.1f}] {sg['text'].strip()}" for sg in transcript]
    system = f"""You edit recorded life-insurance sales calls into short Instagram Reels for OTHER AGENTS to learn from.
The person on the call is agent {agent_name}; the other voice is a client. Output strict JSON only.

Pick 3 to 5 clips, each 30 to 60 seconds, that are real teachable moments for agents: openers, discovery
questions, handling objections, closing, building rapport. Each clip must start and end on complete sentences
and must be self-contained. Prefer stretches where the agent is talking.

NEVER choose a stretch that is mostly health questions, carrier/company talk, or personal/financial details.

Also list EVERY word or phrase anywhere inside your chosen clips that matches this list:
{PROHIBITED}

JSON shape:
{{"clips":[{{"start":<sec>,"end":<sec>,"hook":"<=6 words, banner text","ig_caption":"2-3 sentence Instagram caption + 3 hashtags"}}],
 "client_names":["<first name>","<last name>","<spouse/family names>"],
 "redactions":[{{"text":"<exact words as they appear in the transcript>","type":"health|carrier|location|contact|financial|name"}}]}}

client_names are the client's and their family's names (bleeped on first mention, cut afterwards).
Every other redaction is removed from the audio and captions. Be exhaustive; missing one is a compliance failure."""
    user = 'Transcript with [start-end] seconds per line:\n\n' + '\n'.join(lines)
    j = claude(system, user, 6000)
    clips = [c for c in j.get('clips', []) if isinstance(c, dict) and 20 <= float(c['end']) - float(c['start']) <= 75]
    return clips[:5], [str(n) for n in j.get('client_names', []) if n], j.get('redactions', [])


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
        offset, conf = sync_offset(video, audio, d)
        log.info('%s: offset %.3f confidence %.1f', cid, offset, conf)
        if conf < MIN_SYNC_CONFIDENCE:
            api('engineProblem', callId=cid, notifyAgent=True,
                reason=f'The call recording did not match the phone video (sync confidence {conf:.1f}). Please re-upload the matching pair.')
            return

        log.info('%s: transcribing', cid)
        tpath = os.path.join(d, 'call.json')
        transcript = transcribe(audio, tpath)
        if sum(len(sg['words']) for sg in transcript) < 80:
            api('engineProblem', callId=cid, notifyAgent=True, reason='The call recording was too short or too quiet to transcribe.')
            return

        # only the part of the call that has video behind it (audio t = video t - offset)
        vdur = media_duration(video)
        a_min = max(0.0, -offset) + 0.5
        a_max = vdur - offset - 0.5
        usable = [sg for sg in transcript if sg['start'] >= a_min and sg['end'] <= a_max]
        log.info('%s: video %.0fs, usable audio window %.1f-%.1f s, %d/%d segments', cid, vdur, a_min, a_max, len(usable), len(transcript))
        if sum(len(sg['words']) for sg in usable) < 80:
            api('engineProblem', callId=cid, notifyAgent=True, reason='The video and the call recording barely overlap. Please upload the video and recording of the same call.')
            return

        log.info('%s: picking clips', cid)
        clips, names, redactions = pick_clips(usable, call.get('display_name', 'the agent'))
        clips = [c for c in clips if float(c['start']) >= a_min and float(c['end']) <= a_max]
        cut_spans = redaction_spans(transcript, redactions)
        name_keys = sorted({_norm(n) for n in names if len(_norm(n)) >= 3})
        log.info('%s: %d clips, %d name keys, %d cut spans', cid, len(clips), len(name_keys), len(cut_spans))

        delivered, dropped = [], []
        date = str(call.get('uploaded_at', ''))[:10] or datetime.date.today().isoformat()
        for i, c in enumerate(clips, 1):
            hook = re.sub(r'[\\/:*?"<>|]', '', str(c.get('hook', f'Clip {i}')).strip())[:60] or f'Clip {i}'
            out = os.path.join(d, 'out', f'clip{i}.mp4')
            try:
                res = render({'video': video, 'audio': audio, 'offset': offset, 'transcript': tpath,
                              'start': float(c['start']), 'end': float(c['end']), 'hook': hook, 'out': out,
                              'bleep_words': name_keys, 'cut_spans': cut_spans, 'gap_max': 0.6, 'gap_pad': 0.2})
            except Exception as ex:
                log.warning('%s: clip %d render failed: %s', cid, i, ex); dropped.append(f'{hook}: render failed'); continue
            if not 20 <= res['duration'] <= 75:
                dropped.append(f'{hook}: {res["duration"]:.0f}s after trimming'); continue
            chk = compliance_check(res['caption_text'], hook)
            if chk.get('flagged') or chk.get('unsure'):
                dropped.append(f'{hook}: compliance ({", ".join(map(str, chk.get("items", [])))[:200]})'); continue
            name = f'{date} - {hook}'
            fid = drive_upload(out, name + '.mp4', call['agent_folder_id'], 'video/mp4')
            txt = os.path.join(d, 'out', f'clip{i}.txt')
            open(txt, 'w').write(str(c.get('ig_caption', '')).strip() + '\n')
            drive_upload(txt, name + '.txt', call['agent_folder_id'], 'text/plain')
            delivered.append(fid)
            log.info('%s: delivered "%s" (%.0fs, %d bleeps)', cid, hook, res['duration'], res['bleeps'])

        for dmsg in dropped:
            log.info('%s: dropped -> %s', cid, dmsg)
        if delivered:
            api('engineDone', callId=cid, clipFileIds=delivered, clipCount=len(delivered))
            log.info('%s: done, %d delivered, %d dropped', cid, len(delivered), len(dropped))
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
