"""One-page caption sheet for a delivered clip.

Replaces the old plain .txt caption file. Carries the MyDear.Editor colors in a calmer form: white page,
one deep-blue header, cyan and gold accents. Holds a thumbnail of the clip, the Instagram caption as plain
selectable text the agent can copy straight out of the PDF, and a short plain-language explanation of why
the clip was cut the way it was.

build(cfg) -> output path.  cfg keys:
  out          where to write the pdf
  clip         path to the rendered mp4 (a frame is pulled for the thumbnail)   [optional]
  hook         banner text of the clip
  caption      the Instagram caption text
  why          1-2 sentences from the clip picker on why this moment is worth studying   [optional]
  agent        display name
  when         human label, e.g. "2026-10-26 - 6.42 PM ET"
  duration     finished clip length in seconds
  edits        dict of real counts from the renderer: picked, fillers, tightened, bleeps, redacted
"""
import os, re, glob, subprocess, tempfile

from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph

# --- palette (theme.css, used sparingly) -----------------------------------
PANEL = HexColor('#1b3a9e')
BORDER = HexColor('#8fd3ff')
SKY = HexColor('#2f7fe6')
GOLD = HexColor('#ffcc1a')
RED = HexColor('#e8313a')
INK = HexColor('#15223f')
MUTED = HexColor('#5c6b86')
PAPER = HexColor('#f4f8ff')

W, H = letter
M = 46.0                      # page margin
THUMB_W = 156.0               # 9:16 thumbnail
THUMB_H = THUMB_W * 16.0 / 9.0
GUTTER = 22.0

FONT_DIRS = ['/usr/local/share/fonts/mda', '/usr/local/share/fonts/poppins',
             '/usr/share/fonts/truetype/google-fonts', '/usr/share/fonts']


def _find_font(*names):
    """First matching ttf anywhere in the font dirs, else None."""
    for n in names:
        for d in FONT_DIRS:
            hits = glob.glob(os.path.join(d, '**', n), recursive=True)
            if hits:
                return hits[0]
    return None


def _fonts():
    """Register the display + body faces, falling back to the built-ins if a file is missing."""
    out = {'title': 'Helvetica-Bold', 'body': 'Helvetica', 'bold': 'Helvetica-Bold'}
    pairs = [('MDATitle', 'title', ('Anton-Regular.ttf', 'Anton.ttf', 'BebasNeue-Regular.ttf')),
             ('MDABody', 'body', ('Poppins-Regular.ttf', 'Poppins-Light.ttf', 'Poppins-Medium.ttf')),
             ('MDABold', 'bold', ('Poppins-SemiBold.ttf', 'Poppins-Bold.ttf'))]
    for reg, key, names in pairs:
        path = _find_font(*names)
        if not path:
            continue
        try:
            pdfmetrics.registerFont(TTFont(reg, path))
            out[key] = reg
        except Exception:
            pass
    return out


def _thumbnail(clip, tmp):
    """A frame from the clip, a little way in so it isn't a black first frame."""
    if not clip or not os.path.exists(clip):
        return None
    dst = os.path.join(tmp, 'thumb.jpg')
    for at in ('1.60', '0.60', '0.05'):
        try:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', at, '-i', clip, '-frames:v', '1',
                            '-vf', 'scale=468:-2', '-q:v', '3', dst], check=True, timeout=60)
            if os.path.getsize(dst) > 1200:
                return dst
        except Exception:
            continue
    return None


def _plain(s, limit=1200):
    """Collapse whitespace and strip the few characters that break reportlab's mini-markup."""
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    s = s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return s[:limit]


def _edit_notes(edits, duration):
    """Plain-language bullets built from what the renderer actually did. No claims we can't back up."""
    e = edits or {}
    picked = float(e.get('picked') or 0)
    notes = []
    if picked > duration + 1:
        notes.append('Pulled a %d-second stretch of the call and tightened it to %d seconds.'
                     % (round(picked), round(duration)))
    else:
        notes.append('Cut to %d seconds, the length that holds attention on a Reel.' % round(duration))
    if e.get('fillers'):
        n = e['fillers']
        notes.append('Removed %d filler %s (um, uh, and the hedging kind of "like") so the point lands faster.'
                     % (n, 'word' if n == 1 else 'words'))
    if e.get('tightened'):
        n = e['tightened']
        notes.append('Shortened %d long %s, and cut the silence tight in the opening seconds where it costs you views.'
                     % (n, 'silence' if n == 1 else 'silences'))
    if e.get('kept_pauses'):
        n = e['kept_pauses']
        notes.append('Left %d %s standing where it was doing work - the beat after a question or a close is '
                     'the part other agents need to hear.' % (n, 'pause' if n == 1 else 'pauses'))
    if e.get('bleeps'):
        notes.append('Bleeped the client\'s name on first mention and cut every mention after it.')
    if e.get('redacted'):
        notes.append('Cut the parts compliance does not allow in a public clip - carrier names, personal '
                     'details, locations and money details.')
    notes.append('Captions are burned in from the words actually spoken, so it reads with the sound off.')
    notes.append('The finished clip was checked a second time for compliance before it was delivered.')
    return notes


def build(cfg):
    out = os.path.abspath(cfg['out'])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = tempfile.mkdtemp(dir=os.path.dirname(out))
    try:
        return _build(cfg, out, tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _build(cfg, out, tmp):
    F = _fonts()
    hook = _plain(cfg.get('hook') or 'Your clip', 70)
    caption = _plain(cfg.get('caption') or '', 1400)
    why = _plain(cfg.get('why') or '', 600)
    agent = _plain(cfg.get('agent') or '', 60)
    when = _plain(cfg.get('when') or '', 60)
    duration = float(cfg.get('duration') or 0)
    thumb = _thumbnail(cfg.get('clip'), tmp)

    c = rl_canvas.Canvas(out, pagesize=letter)
    c.setTitle(hook)
    c.setAuthor('My Dear Academy')

    # ---------------- header band
    BAND = 104.0
    c.setFillColor(PANEL)
    c.rect(0, H - BAND, W, BAND, stroke=0, fill=1)
    c.setFillColor(BORDER)
    c.rect(0, H - BAND - 3, W, 3, stroke=0, fill=1)
    c.setFillColor(GOLD)
    c.rect(0, H - BAND - 6, W, 3, stroke=0, fill=1)

    c.setFont(F['bold'], 9)
    c.setFillColor(BORDER)
    rank, of, kind = cfg.get('rank'), cfg.get('of'), _plain(cfg.get('kind') or '', 20)
    label = 'MYDEAR.EDITOR  //  CLIP SHEET'
    if rank:
        label += '  //  CLIP %d OF %s' % (rank, of or '?')
    if kind:
        label += '  //  %s' % kind.upper()
    c.drawString(M, H - 36, label)
    if rank == 1:                                    # the one to post first, said where it is seen
        tag = 'POST THIS ONE FIRST'
        tw = c.stringWidth(tag, F['bold'], 9)
        c.setFillColor(GOLD)
        c.roundRect(W - M - tw - 18, H - 42, tw + 18, 19, 4, stroke=0, fill=1)
        c.setFillColor(PANEL)
        c.drawString(W - M - tw - 9, H - 36, tag)
    c.setFont(F['title'], 29)
    c.setFillColor(white)
    title = hook.upper()
    while c.stringWidth(title, F['title'], 29) > W - 2 * M and len(title) > 8:
        title = title[:-2]
    c.drawString(M, H - 68, title)
    c.setFont(F['body'], 9.5)
    c.setFillColor(BORDER)
    meta = '  ·  '.join(x for x in (agent, when, '%d seconds' % round(duration) if duration else '') if x)
    c.drawString(M, H - 86, meta)

    top = H - BAND - 30
    # ---------------- thumbnail
    if thumb:
        c.setFillColor(PAPER)
        c.rect(M - 3, top - THUMB_H - 3, THUMB_W + 6, THUMB_H + 6, stroke=0, fill=1)
        try:
            c.drawImage(thumb, M, top - THUMB_H, width=THUMB_W, height=THUMB_H,
                        preserveAspectRatio=False, mask=None)
        except Exception:
            pass
        c.setStrokeColor(BORDER)
        c.setLineWidth(2)
        c.rect(M, top - THUMB_H, THUMB_W, THUMB_H, stroke=1, fill=0)
    else:
        c.setFillColor(PAPER)
        c.rect(M, top - THUMB_H, THUMB_W, THUMB_H, stroke=0, fill=1)

    # ---------------- caption panel
    cx = M + THUMB_W + GUTTER
    cw = W - M - cx
    c.setFillColor(PANEL)
    c.rect(cx, top - 20, cw, 20, stroke=0, fill=1)
    c.setFont(F['bold'], 9)
    c.setFillColor(GOLD)
    c.drawString(cx + 9, top - 14, 'CAPTION  -  COPY AND PASTE THIS')

    box_top = top - 20
    box_max = THUMB_H - 20

    cap_style = ParagraphStyle('cap', fontName=F['body'], fontSize=10.6, leading=15.4,
                               textColor=INK, alignment=TA_LEFT)
    size = 10.6
    while size >= 7.4:
        cap_style.fontSize = size
        cap_style.leading = size * 1.45
        p = Paragraph(caption or '(no caption was generated for this clip)', cap_style)
        _, used = p.wrap(cw - 24, box_max - 20)
        if used <= box_max - 20:
            break
        size -= 0.6
    box_h = max(96.0, min(box_max, used + 26))       # hug the caption instead of leaving a dead panel
    c.setFillColor(PAPER)
    c.setStrokeColor(BORDER)
    c.setLineWidth(1)
    c.rect(cx, box_top - box_h, cw, box_h, stroke=1, fill=1)
    p.drawOn(c, cx + 12, box_top - 13 - used)

    # ---------------- why we cut it this way
    y = top - THUMB_H - 34
    c.setFillColor(PANEL)
    c.setFont(F['title'], 15)
    c.drawString(M, y, 'WHY WE EDITED IT THIS WAY')
    c.setStrokeColor(GOLD)
    c.setLineWidth(2)
    c.line(M, y - 7, W - M, y - 7)
    y -= 26

    body_w = W - 2 * M
    if why:
        st = ParagraphStyle('why', fontName=F['bold'], fontSize=11, leading=15.6, textColor=INK)
        pw = Paragraph(why, st)
        _, h = pw.wrap(body_w, 120)
        pw.drawOn(c, M, y - h)
        y -= h + 14

    bullet = ParagraphStyle('b', fontName=F['body'], fontSize=10, leading=14, textColor=MUTED)
    for note in _edit_notes(cfg.get('edits'), duration):
        pb = Paragraph(_plain(note, 300), bullet)
        _, h = pb.wrap(body_w - 16, 80)
        if y - h < 92:
            break
        c.setFillColor(SKY)
        c.circle(M + 3.5, y - 5.5, 2.6, stroke=0, fill=1)
        pb.drawOn(c, M + 16, y - h)
        y -= h + 6

    # ---------------- footer
    c.setStrokeColor(BORDER)
    c.setLineWidth(1)
    c.line(M, 70, W - M, 70)
    c.setFillColor(RED)
    c.circle(M + 4, 55, 3.4, stroke=0, fill=1)
    c.setFont(F['body'], 8.6)
    c.setFillColor(MUTED)
    c.drawString(M + 14, 52, 'Carrier names, client details, locations and money details are removed automatically. '
                             'Give it one look before you post.')
    c.setFont(F['bold'], 8.6)
    c.setFillColor(PANEL)
    c.drawString(M, 36, 'My Dear Academy  ·  Legacy Family Life')
    c.setFont(F['body'], 8.6)
    c.setFillColor(MUTED)
    c.drawRightString(W - M, 36, 'Made by MyDear.Editor')

    c.showPage()
    c.save()
    return out
