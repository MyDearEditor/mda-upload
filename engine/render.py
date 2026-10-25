"""Render one Reel clip. Importable (render(cfg)) or CLI: python3 -I render.py clip_config.json

config keys:
  video, audio            source files
  offset                  seconds; audio t=0 sits at video t=offset (global estimate; refined locally here)
  transcript              path to transcript json (list of segments with words)
  start, end              clip span in AUDIO time
  hook                    banner text (optional)
  out                     output mp4 path
  bleep_words             lowercase name fragments: first mention in the clip is bleeped, later mentions are cut
  cut_spans               [[s,e],...] audio-time spans whose words are removed entirely
  gap_max, gap_pad        dead-air trimming (default 0.6 / 0.2)
Returns {"out", "duration", "caption_text", "bleeps", "cuts"}.
"""
import json, sys, subprocess, re, os, tempfile, shutil
import numpy as np

SR = 48000
FPS = 30
RED = '&H002B1BE0'   # BGR for #E01B2B


def _pcm(path, start, dur, sr):
    r = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{max(0, start):.3f}', '-t', f'{dur:.3f}', '-i', path,
                        '-vn', '-ac', '1', '-ar', str(sr), '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(r, np.float32).copy()


def _clean(s):
    return re.sub(r"[^\w'%$-]", '', s).upper()


def _ts(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def render(cfg):
    out_path = os.path.abspath(cfg['out'])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = tempfile.mkdtemp(dir=os.path.dirname(out_path))
    try:
        return _render(cfg, out_path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _render(cfg, out_path, tmp):
    # ---- words in range
    words = []
    for seg in json.load(open(cfg['transcript'])):
        for w in seg['words']:
            if w['s'] >= cfg['start'] - 0.05 and w['e'] <= cfg['end'] + 0.05:
                words.append(dict(w))
    if not words:
        raise ValueError('no words in clip range')

    name_keys = [k.lower() for k in cfg.get('bleep_words', []) if k]
    cut_spans = cfg.get('cut_spans', [])
    seen = False
    for w in words:
        w['bleep'] = w['cut'] = False
        if any(cs[0] <= w['s'] < cs[1] or cs[0] < w['e'] <= cs[1] for cs in cut_spans):
            w['cut'] = True
            continue
        if name_keys and any(k in re.sub(r'[^a-z]', '', w['w'].lower()) for k in name_keys):
            if seen: w['cut'] = True
            else: w['bleep'] = seen = True

    # ---- keep-segments: trim dead air and drop cut words
    gap_max, pad = cfg.get('gap_max', 0.6), cfg.get('gap_pad', 0.2)
    kept = [w for w in words if not w['cut']]
    if len(kept) < 5:
        raise ValueError('clip has too few words left after redaction')
    segs = []
    a = max(cfg['start'], kept[0]['s'] - pad)
    for prev, nxt in zip(kept, kept[1:]):
        cw = [w for w in words if w['cut'] and prev['e'] <= w['s'] < nxt['s']]
        if cw:
            segs.append([a, max(prev['e'] + 0.05, min(prev['e'] + pad, cw[0]['s'] - 0.02))])
            a = min(nxt['s'] - 0.05, max(nxt['s'] - pad, cw[-1]['e'] + 0.02))
        elif nxt['s'] - prev['e'] > gap_max:
            segs.append([a, prev['e'] + pad]); a = nxt['s'] - pad
    segs.append([a, min(cfg['end'], kept[-1]['e'] + 0.4)])
    words = kept
    A0 = segs[0][0]
    A1 = segs[-1][1]

    # ---- refine sync offset locally (handles slow drift on long calls)
    off = cfg['offset']
    try:
        from scipy.signal import correlate
        W = min(20.0, A1 - A0)
        ca = _pcm(cfg['audio'], A0, W, 8000)
        va = _pcm(cfg['video'], A0 + off - 1.0, W + 2.0, 8000)
        if len(va) > len(ca) > 8000:
            c = correlate(va - va.mean(), ca - ca.mean(), mode='valid')
            off = off - 1.0 + np.argmax(c) / 8000
    except Exception as ex:
        print('offset refine skipped:', ex)

    # ---- snap every cut to the frame grid so audio and video stay the same length
    segs = [[A0 + round((s0 - A0) * FPS) / FPS, A0 + round((e0 - A0) * FPS) / FPS] for s0, e0 in segs]
    segs = [x for x in segs if x[1] > x[0]]
    A1 = segs[-1][1]
    total = sum(e0 - s0 for s0, e0 in segs)

    def to_out(t):
        acc = 0.0
        for s, e in segs:
            if t <= e:
                return acc + max(0.0, t - s)
            acc += e - s
        return acc

    # ---- call audio for this clip only, with bleeps
    aud = _pcm(cfg['audio'], A0, A1 - A0, SR)
    for w in words:
        if w['bleep']:
            i0, i1 = max(0, int((w['s'] - 0.04 - A0) * SR)), min(len(aud), int((w['e'] + 0.04 - A0) * SR))
            t = np.arange(i1 - i0) / SR
            aud[i0:i1] = 0.25 * np.sin(2 * np.pi * 1000 * t)
    F = int(0.01 * SR); ramp = np.linspace(0, 1, F, dtype=np.float32)
    parts = []
    for s0, e0 in segs:
        p_ = aud[int((s0 - A0) * SR):int((e0 - A0) * SR)].copy()
        if len(p_) > 2 * F:
            p_[:F] *= ramp; p_[-F:] *= ramp[::-1]
        parts.append(p_)
    bleeped = os.path.join(tmp, 'bleeped.f32')
    np.concatenate(parts).astype(np.float32).tofile(bleeped)

    # ---- captions (ASS): white bold on red box, 5-7 words per card
    MINW, MAXW = cfg.get('cap_min_words', 5), cfg.get('cap_max_words', 7)
    cards, cur = [], []
    for i, w in enumerate(words):
        txt = '****' if w['bleep'] else _clean(w['w'])
        if not txt: continue
        if txt.startswith('-') and cur:
            cur[-1] = (cur[-1][0], cur[-1][1] + txt)
        else:
            cur.append((w, txt))
        nxt = words[i + 1] if i + 1 < len(words) else None
        sentence_end = re.search(r'[.?!]$', w['w'].strip())
        pause = nxt is not None and nxt['s'] - w['e'] > 0.5
        rest = 0
        for x in words[i + 1:]:
            rest += 1
            if re.search(r'[.?!]$', x['w'].strip()): break
        short_tail = 0 < rest <= 2 and len(cur) + rest <= MAXW + 2
        full = (len(cur) >= MAXW and not short_tail) or len(cur) >= MAXW + 2
        if sentence_end or pause or full or (len(cur) >= MINW and re.search(r',$', w['w'].strip())):
            cards.append(cur); cur = []
    if cur: cards.append(cur)

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Poppins,72,&H00FFFFFF,&H00FFFFFF,{RED},{RED},-1,0,0,0,100,100,0,0,3,14,0,2,90,90,{cfg.get('cap_margin_v', 330)},1
Style: Hook,Poppins,66,&H00FFFFFF,&H00FFFFFF,{RED},{RED},-1,0,0,0,100,100,0,0,3,18,0,8,90,90,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if cfg.get('hook'):
        ass += f"Dialogue: 1,{_ts(0)},{_ts(total)},Hook,,0,0,0,,{cfg['hook'].upper()}\n"
    for i, c in enumerate(cards):
        s = to_out(c[0][0]['s'])
        e = to_out(cards[i + 1][0][0]['s']) if i + 1 < len(cards) else total
        e = min(e, to_out(c[-1][0]['e']) + 0.6)
        ass += f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\fscx92\\fscy92\\t(0,80,\\fscx100\\fscy100)}}{' '.join(x[1] for x in c)}\n"
    ass_path = os.path.join(tmp, 'caps.ass')
    open(ass_path, 'w').write(ass)

    # ---- ffmpeg graph
    V0 = A0 + off
    vsel = '+'.join(f"gte(t\\,{s-A0-0.5/FPS:.4f})*lt(t\\,{e-A0-0.5/FPS:.4f})" for s, e in segs)
    ass_esc = ass_path.replace('\\', '/').replace(':', '\\:')
    fc = [f"[0:v]fps={FPS},select='{vsel}',setpts=N/{FPS}/TB[vc]",
          "[1:a]anull[ac]",
          f"[vc]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,ass={ass_esc}[vo]",
          "[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[ao]"]
    cmd = ['ffmpeg', '-v', 'error', '-y', '-ss', f'{V0:.3f}', '-t', f'{A1-A0+0.5:.3f}', '-i', cfg['video'],
           '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', bleeped,
           '-filter_complex', ';'.join(fc), '-map', '[vo]', '-map', '[ao]',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', str(FPS),
           '-c:a', 'aac', '-b:a', '192k', '-ac', '2', '-movflags', '+faststart', out_path]
    subprocess.run(cmd, check=True)
    caption_text = ' '.join(' '.join(x[1] for x in c) for c in cards)
    return {'out': out_path, 'duration': total, 'caption_text': caption_text,
            'bleeps': sum(w['bleep'] for w in words), 'cuts': len(segs) - 1}


if __name__ == '__main__':
    print(json.dumps(render(json.load(open(sys.argv[1]))), indent=1))
