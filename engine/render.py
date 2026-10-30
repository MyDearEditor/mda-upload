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
  gap_max, gap_pad        dead-air trimming (default 0.5 / 0.15)
  cut_fillers             remove um/uh, stutters, and clearly-filler "like" / "you know" (default True)
Returns {"out", "duration", "caption_text", "bleeps", "cuts", "picked", "fillers", "redacted", "tightened"}
(the last four describe what the edit actually did, for the clip sheet PDF).
"""
import json, sys, subprocess, re, os, tempfile, shutil
import numpy as np

SR = 48000
FPS = 30
RED = '&H002B1BE0'   # BGR for #E01B2B
BLEEP_LEVEL = 0.175  # 30% quieter than the original 0.25 - audible, not startling
HOOK_SECONDS = 8.0   # the banner lands and leaves; it is a hook, not a watermark
ZOOM_MAX = 1.20      # how far in to push while the agent is talking
ZOOM_EASE = 1.2      # seconds to ride in and out: a slow, deliberate glide, not a snap
ZOOM_MIN_TURN = 2.6  # a turn must fit the glide in, a beat at full push, and the glide out
ZOOM_JOIN = 0.9      # merge agent turns separated by less than this
ZOOM_MAX_TURNS = 40  # keep the filter expression to a sane length

_ZSCALE = None  # does this ffmpeg have the zscale filter? (checked once)


def _has_zscale():
    global _ZSCALE
    if _ZSCALE is None:
        try:
            out = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True).stdout
            _ZSCALE = ' zscale ' in out
        except Exception:
            _ZSCALE = False
    return _ZSCALE


def _hdr_tonemap(video):
    """Phones record HDR (HLG or PQ). Re-encoding that as plain SDR washes the clip out with a green
    cast, so when the source is HDR, convert it down to BT.709 properly. Returns a filter prefix
    ('' when the source is already SDR, or when this ffmpeg cannot do the conversion)."""
    try:
        trc = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                              '-show_entries', 'stream=color_transfer', '-of', 'csv=p=0', video],
                             capture_output=True, text=True).stdout.strip().lower()
    except Exception:
        return ''
    if trc not in ('arib-std-b67', 'smpte2084'):
        return ''
    if not _has_zscale():
        print('render: HDR source but ffmpeg has no zscale - colors may look washed out', file=sys.stderr)
        return ''
    return ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
            'tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,')


def _zoom_expr(turns, total):
    """ffmpeg zoompan z-expression: 1.0 by default, easing up to ZOOM_MAX while the agent speaks.

    turns are (start, end) in OUTPUT time. zoompan has no t variable, but output fps is fixed, so
    time is on/FPS. Commas inside the expression are escaped because the filtergraph splits on them.
    """
    merged = []
    for s, e in sorted(turns):
        s, e = max(0.0, float(s)), min(float(total), float(e))
        if e - s < ZOOM_MIN_TURN:
            continue
        if merged and s - merged[-1][1] < ZOOM_JOIN:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    merged = [m for m in merged if m[1] - m[0] >= ZOOM_MIN_TURN][:ZOOM_MAX_TURNS]
    if not merged:
        return None, 0
    T = f'(on/{FPS})'
    parts = []
    for s, e in merged:
        parts.append(f'between({T}\\,{s:.2f}\\,{e:.2f})*'
                     f'max(0\\,min(1\\,min(({T}-{s:.2f})/{ZOOM_EASE}\\,({e:.2f}-{T})/{ZOOM_EASE})))')
    return f'1+{ZOOM_MAX - 1:.3f}*({"+".join(parts)})', len(merged)


def _font_file(name):
    """Where fontconfig says this family's bold face lives (what libass itself will use)."""
    try:
        out = subprocess.run(['fc-match', '-f', '%{file}', f'{name}:bold'],
                             capture_output=True, text=True).stdout.strip()
        return out or None
    except Exception:
        return None


def _measurer(font_name, size):
    """Pixel-width function for text at the ASS script resolution, for sizing drawn boxes.
    Falls back to a character estimate when the font cannot be measured."""
    try:
        from PIL import ImageFont
        f = ImageFont.truetype(_font_file(font_name), size)
        return lambda t: float(f.getlength(t))
    except Exception:
        return lambda t: len(t) * size * 0.56


def _wrap(meas, text, maxw):
    """Greedy word-wrap against a measurer; every caption line must fit its own box."""
    lines, cur = [], ''
    for word in text.split():
        trial = (cur + ' ' + word).strip()
        if cur and meas(trial) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _rrect(w, h, r=20):
    """ASS vector path for a w x h rounded rectangle starting at 0,0 (anchored via \\an5)."""
    w, h = int(round(w)), int(round(h))
    r = int(min(r, h / 2, w / 2))
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} "
            f"b {w} {h} {w} {h} {w - r} {h} l {r} {h} b 0 {h} 0 {h} 0 {h - r} "
            f"l 0 {r} b 0 0 0 0 {r} 0")


def _pcm(path, start, dur, sr):
    r = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{max(0, start):.3f}', '-t', f'{dur:.3f}', '-i', path,
                        '-vn', '-ac', '1', '-ar', str(sr), '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(r, np.float32).copy()


def _clean(s):
    return re.sub(r"[^\w'%$-]", '', s).upper()


FILLERS = {'um', 'uh', 'uhm', 'umm', 'uhh', 'er', 'erm', 'ah', 'ahh', 'hmm', 'hm', 'mhm', 'mmhmm', 'mm'}
# "like" right after one of these is almost always meaningful ("would like", "looks like", "I like") — keep it
LIKE_SAFE_BEFORE = {'would', 'wouldnt', 'd', 'id', 'youd', 'wed', 'theyd', 'shed', 'hed', 'i', 'you', 'we', 'they',
                    'she', 'he', 'people', 'dont', 'didnt', 'much', 'just', 'really', 'anything', 'something',
                    'nothing', 'things', 'stuff', 'feel', 'feels', 'felt', 'look', 'looks', 'looked', 'looking',
                    'seem', 'seems', 'seemed', 'sound', 'sounds', 'sounded', 'is', 'was', 'be', 'more', 'kind', 'sort'}


def _wkey(w):
    return re.sub(r"[^a-z]", '', w.lower())


def _mark_fillers(words):
    """Mark filler words as cut: um/uh always; back-to-back stutters; 'like' and 'you know' only when they
    read as hedges (a pause or comma on a side) so meaningful uses survive."""
    for i, w in enumerate(words):
        if w['cut'] or w['bleep']:
            continue
        t = _wkey(w['w'])
        prev = words[i - 1] if i else None
        nxt = words[i + 1] if i + 1 < len(words) else None
        if t in FILLERS:
            w['cut'] = True
            w['why'] = 'filler'
            continue
        # stutter: the same word twice in a row with no real pause ("I I think", "the the") — drop the first
        if (nxt is not None and t and _wkey(nxt['w']) == t and not nxt['cut']
                and nxt['s'] - w['e'] < 0.35 and not re.search(r'[.?!]$', w['w'].strip())):
            w['cut'] = True
            w['why'] = 'filler'
            continue
        prev_t = _wkey(prev['w']) if prev else ''
        prev_comma = bool(prev) and bool(re.search(r'[,–—]$', prev['w'].strip()))
        if t == 'like':
            pause_before = prev is None or w['s'] - prev['e'] > 0.28 or prev_comma
            pause_after = nxt is None or nxt['s'] - w['e'] > 0.28 or bool(re.search(r',$', w['w'].strip()))
            # bracketed on both sides it is a hedge no matter what came before; one-sided only when
            # the previous word doesn't make it meaningful ("would like", "looks like", "I like")
            if (pause_before and pause_after) or (prev_t not in LIKE_SAFE_BEFORE and (pause_before or pause_after)):
                w['cut'] = True
                w['why'] = 'filler'
                continue
        # parenthetical "you know": pauses or commas around the pair
        if t == 'you' and nxt is not None and _wkey(nxt['w']) == 'know' and not nxt['cut']:
            after = words[i + 2] if i + 2 < len(words) else None
            pb = prev is None or w['s'] - prev['e'] > 0.25 or prev_comma
            pa = after is None or after['s'] - nxt['e'] > 0.25 or re.search(r'[,.?!]$', nxt['w'].strip())
            if pb and pa:
                w['cut'] = nxt['cut'] = True
                w['why'] = nxt['why'] = 'filler'


def _ts(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def words_in_range(transcript, start, end):
    """Every transcribed word that sits wholly inside [start, end], as fresh dicts."""
    out = []
    for seg in json.load(open(transcript)) if isinstance(transcript, str) else transcript:
        for w in seg['words']:
            if w['s'] >= start - 0.05 and w['e'] <= end + 0.05:
                out.append(dict(w))
    return out


def mark_words(words, bleep_words=(), cut_spans=(), cut_fillers=True, name_mode='bleep'):
    """Tag each word cut/bleep and why. Redactions and repeat names go first, then fillers.

    name_mode 'bleep' (default): quiet tone over the first mention, later mentions cut.
    name_mode 'cut': every mention is removed outright - no tone anywhere."""
    name_keys = [k.lower() for k in bleep_words if k]
    seen = False
    for w in words:
        w['bleep'] = w['cut'] = False
        w['why'] = ''
        if any(cs[0] <= w['s'] < cs[1] or cs[0] < w['e'] <= cs[1] for cs in cut_spans):
            w['cut'] = True
            w['why'] = 'redaction'
            continue
        if name_keys and any(k in re.sub(r'[^a-z]', '', w['w'].lower()) for k in name_keys):
            if seen or name_mode == 'cut':
                w['cut'] = True
                w['why'] = 'name'
                seen = True
            else:
                w['bleep'] = seen = True
    if cut_fillers:
        _mark_fillers(words)
    return words


def _gap_allow(prev, out_t, base):
    """How much silence to leave standing after this word.

    One threshold for the whole clip was wrong in both directions: dead air in the opening seconds
    loses the viewer, while the beat after a question or a close is the thing other agents need to hear.
    """
    if out_t < 3.0:
        return 0.22                      # open tight - the first seconds decide whether anyone stays
    t = prev['w'].strip()
    if t.endswith('?'):
        return 1.30                      # the silence after a question is the technique, not dead air
    if t.endswith(('.', '!')):
        return 0.80                      # let a finished thought land
    return base


def plan_segments(words, start, end, gap_max=0.5, pad=0.15, tail=0.4):
    """Work out which stretches survive, without touching ffmpeg.

    Returns (segs, total_seconds, stats) or None if too little is left. The engine calls this to
    predict a clip's finished length before committing to a render, so a clip can be sized by what
    it will ACTUALLY be, not by the span the picker guessed.
    """
    kept = [w for w in words if not w['cut']]
    if len(kept) < 5:
        return None
    segs, tightened, kept_pauses = [], 0, 0
    a = max(start, kept[0]['s'] - pad)
    out_t = 0.0
    for prev, nxt in zip(kept, kept[1:]):
        cw = [w for w in words if w['cut'] and prev['e'] <= w['s'] < nxt['s']]
        if cw:
            e0 = max(prev['e'] + 0.05, min(prev['e'] + pad, cw[0]['s'] - 0.02))
            segs.append([a, e0])
            out_t += max(0.0, e0 - a)
            a = min(nxt['s'] - 0.05, max(nxt['s'] - pad, cw[-1]['e'] + 0.02))
        else:
            gap = nxt['s'] - prev['e']
            if gap > _gap_allow(prev, out_t + max(0.0, prev['e'] - a), gap_max):
                e0 = prev['e'] + pad
                segs.append([a, e0])
                out_t += max(0.0, e0 - a)
                a = nxt['s'] - pad
                tightened += 1
            elif gap > gap_max:
                kept_pauses += 1
    segs.append([a, min(end, kept[-1]['e'] + tail)])
    segs = [s for s in segs if s[1] > s[0]]
    if not segs:
        return None
    total = sum(e - s for s, e in segs)
    return segs, total, {'tightened': tightened, 'kept_pauses': kept_pauses}


def predict_duration(transcript, start, end, bleep_words=(), cut_spans=(), gap_max=0.5, pad=0.15, name_mode='bleep'):
    """Finished length this span would deliver, or None. Same maths the renderer uses."""
    words = words_in_range(transcript, start, end)
    if not words:
        return None
    mark_words(words, bleep_words, cut_spans, name_mode=name_mode)
    plan = plan_segments(words, start, end, gap_max, pad)
    return None if plan is None else plan[1]


def render(cfg):
    out_path = os.path.abspath(cfg['out'])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = tempfile.mkdtemp(dir=os.path.dirname(out_path))
    try:
        return _render(cfg, out_path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _render(cfg, out_path, tmp):
    # ---- words in range, marked for cutting (same helpers the engine predicts with)
    words = words_in_range(cfg['transcript'], cfg['start'], cfg['end'])
    if not words:
        raise ValueError('no words in clip range')
    mark_words(words, cfg.get('bleep_words', []), cfg.get('cut_spans', []), cfg.get('cut_fillers', True),
               name_mode=cfg.get('name_mode', 'bleep'))

    # ---- keep-segments: drop cut words, tighten dead air, leave the pauses that do work
    gap_max, pad = cfg.get('gap_max', 0.5), cfg.get('gap_pad', 0.15)
    plan = plan_segments(words, cfg['start'], cfg['end'], gap_max, pad)
    if plan is None:
        raise ValueError('clip has too few words left after redaction')
    segs, _, stats = plan
    tightened, kept_pauses = stats['tightened'], stats['kept_pauses']
    # counts for the clip sheet, taken before `words` is narrowed to the survivors
    n_filler = sum(1 for w in words if w['cut'] and w.get('why') == 'filler')
    n_redact = sum(1 for w in words if w['cut'] and w.get('why') in ('redaction', 'name'))
    words = [w for w in words if not w['cut']]
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
            tone = BLEEP_LEVEL * np.sin(2 * np.pi * 1000 * t)
            # ease the tone in and out: a hard-edged sine is half of what makes a bleep grating
            r = int(min(0.008 * SR, len(tone) // 2))
            if r > 1:
                tone[:r] *= np.linspace(0, 1, r)
                tone[-r:] *= np.linspace(1, 0, r)
            aud[i0:i1] = tone
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

    st = cfg.get('style') or {}
    def bgr(hexcol, default):
        h = str(hexcol or default).lstrip('#')
        if len(h) != 6: h = default.lstrip('#')
        return '&H00' + h[4:6] + h[2:4] + h[0:2]
    font = st.get('font') or 'Poppins'
    cap_text, cap_box = bgr(st.get('text'), '#FFFFFF'), bgr(st.get('box'), '#E01B2B')
    hook_text, hook_box = bgr(st.get('hook_text'), '#FFFFFF'), bgr(st.get('hook_box'), '#E01B2B')
    margin_v = {'low': 330, 'mid': 520, 'high': 760}.get(st.get('position'), cfg.get('cap_margin_v', 330))
    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},72,{cap_text},{cap_text},{cap_box},{cap_box},-1,0,0,0,100,100,0,0,3,14,0,2,90,90,{margin_v},1
Style: Hook,{font},66,{hook_text},{hook_text},{hook_box},{hook_box},-1,0,0,0,100,100,0,0,3,18,0,8,90,90,200,1
Style: CapR,{font},72,{cap_text},{cap_text},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: BoxCap,{font},72,{cap_box},{cap_box},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: HookR,{font},66,{hook_text},{hook_text},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: BoxHook,{font},66,{hook_box},{hook_box},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rounded = str(st.get('corners', '')).lower() == 'rounded'
    meas72 = _measurer(font, 72) if rounded else None
    meas66 = _measurer(font, 66) if rounded else None
    if cfg.get('hook'):
        hook_end = min(float(cfg.get('hook_seconds', HOOK_SECONDS)), total)
        if rounded:
            yc = 244.0                                     # first hook line sits where the banner used to
            for ln in _wrap(meas66, cfg['hook'].upper(), 840):
                bw = meas66(ln) + 56
                ass += (f"Dialogue: 1,{_ts(0)},{_ts(hook_end)},BoxHook,,0,0,0,,"
                        f"{{\\fad(0,350)\\an5\\pos(540,{yc:.0f})\\p1}}{_rrect(bw, 88)}{{\\p0}}\n")
                ass += (f"Dialogue: 2,{_ts(0)},{_ts(hook_end)},HookR,,0,0,0,,"
                        f"{{\\fad(0,350)\\an5\\pos(540,{yc:.0f})}}{ln}\n")
                yc += 98
        else:
            ass += (f"Dialogue: 1,{_ts(0)},{_ts(hook_end)},Hook,,0,0,0,,"
                    f"{{\\fad(0,350)}}{cfg['hook'].upper()}\n")
    for i, c in enumerate(cards):
        s = to_out(c[0][0]['s'])
        e = to_out(cards[i + 1][0][0]['s']) if i + 1 < len(cards) else total
        e = min(e, to_out(c[-1][0]['e']) + 0.6)
        if rounded:
            H, GAP = 96, 10
            lines = _wrap(meas72, ' '.join(x[1] for x in c), 840)
            yc = 1920 - margin_v - H / 2 + 14 - (len(lines) - 1) * (H + GAP)
            for ln in lines:
                bw = meas72(ln) + 60
                ass += (f"Dialogue: 0,{_ts(s)},{_ts(e)},BoxCap,,0,0,0,,"
                        f"{{\\an5\\pos(540,{yc:.0f})\\p1}}{_rrect(bw, H)}{{\\p0}}\n")
                ass += f"Dialogue: 1,{_ts(s)},{_ts(e)},CapR,,0,0,0,,{{\\an5\\pos(540,{yc:.0f})}}{ln}\n"
                yc += H + GAP
        else:
            ass += f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\fscx92\\fscy92\\t(0,80,\\fscx100\\fscy100)}}{' '.join(x[1] for x in c)}\n"
    ass_path = os.path.join(tmp, 'caps.ass')
    open(ass_path, 'w').write(ass)

    # ---- ffmpeg graph
    V0 = A0 + off
    vsel = '+'.join(f"gte(t\\,{s-A0-0.5/FPS:.4f})*lt(t\\,{e-A0-0.5/FPS:.4f})" for s, e in segs)
    ass_esc = ass_path.replace('\\', '/').replace(':', '\\:')
    # optional push-in while the agent is talking: agent turns arrive in audio time, map them to output time
    zexpr, zturns = None, 0
    if cfg.get('zoom_turns'):
        zexpr, zturns = _zoom_expr([(to_out(s), to_out(e)) for s, e in cfg['zoom_turns']], total)
    zoom_filter = ('' if not zexpr else
                   f",zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps={FPS}")

    tm = _hdr_tonemap(cfg['video'])                       # '' for SDR sources
    fc = [f"[0:v]fps={FPS},select='{vsel}',setpts=N/{FPS}/TB[vc]",
          "[1:a]anull[ac]",
          f"[vc]{tm}scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
          f"{zoom_filter},setsar=1,ass={ass_esc}[vo]",
          "[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[ao]"]
    cmd = ['ffmpeg', '-v', 'error', '-y', '-ss', f'{V0:.3f}', '-t', f'{A1-A0+0.5:.3f}', '-i', cfg['video'],
           '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', bleeped,
           '-filter_complex', ';'.join(fc), '-map', '[vo]', '-map', '[ao]',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', str(FPS),
           '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
           '-c:a', 'aac', '-b:a', '192k', '-ac', '2', '-movflags', '+faststart', out_path]
    subprocess.run(cmd, check=True)
    caption_text = ' '.join(' '.join(x[1] for x in c) for c in cards)
    return {'out': out_path, 'duration': total, 'caption_text': caption_text,
            'bleeps': sum(w['bleep'] for w in words), 'cuts': len(segs) - 1,
            'picked': float(cfg['end']) - float(cfg['start']),
            'fillers': n_filler, 'redacted': n_redact, 'tightened': tightened,
            'kept_pauses': kept_pauses, 'zooms': zturns}


if __name__ == '__main__':
    print(json.dumps(render(json.load(open(sys.argv[1]))), indent=1))
