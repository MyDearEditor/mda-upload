#!/usr/bin/env python3
"""One-off diagnostic: how separable are the two voices on an already-uploaded call?

The live check in engine.py measures this per transcribed word, which means paying for a
transcription before you can see the answer. This measures the same thing on fixed frames of
voiced audio instead, so a call can be examined in seconds, and prints the distribution rather
than a verdict - the point is to choose the thresholds from real calls instead of guessing.

  set -a; . /opt/mda/.env; set +a
  /opt/mda/venv/bin/python3 -I /opt/mda/repo/engine/spkcheck.py <call_id>
"""
import os, sys, json, tempfile, shutil
import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E                                        # noqa: E402  (needs MDA_* env)

HOP = 0.25                                                # seconds per frame


def drive_get(url, token, **params):
    r = requests.get(url, headers={'Authorization': 'Bearer ' + token}, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def find_pair(call_id, token):
    """The raw video/audio pair for this call, from its folder under Admin/Raw Uploads."""
    q = "mimeType='application/vnd.google-apps.folder' and name='%s' and trashed=false" % call_id
    folders = drive_get('https://www.googleapis.com/drive/v3/files', token, q=q,
                        fields='files(id,name)', supportsAllDrives='true',
                        includeItemsFromAllDrives='true').get('files', [])
    if not folders:
        raise SystemExit('no raw folder named %s - it may be past the 7 day cleanup' % call_id)
    kids = drive_get('https://www.googleapis.com/drive/v3/files', token,
                     q="'%s' in parents and trashed=false" % folders[0]['id'],
                     fields='files(id,name,size,mimeType)', supportsAllDrives='true',
                     includeItemsFromAllDrives='true').get('files', [])
    vid = next((k for k in kids if 'video' in k.get('mimeType', '') or
                k['name'].lower().endswith(('.mov', '.mp4', '.m4v'))), None)
    aud = next((k for k in kids if k is not vid and not k['name'].endswith('.json')), None)
    if not vid or not aud:
        raise SystemExit('could not spot the pair in %s: %s' % (call_id, [k['name'] for k in kids]))
    return vid, aud


def stats(x, grp, times):
    """Separation between the two clusters, and how well the labels hold together over time."""
    spread = float(x[~grp].std() + x[grp].std()) + 1e-6
    sep = (float(x[grp].mean()) - float(x[~grp].mean())) / spread
    pairs = same = 0
    for i in range(1, len(times)):
        if times[i] - times[i - 1] <= 1.5:
            pairs += 1
            same += bool(grp[i] == grp[i - 1])
    return sep, (same / pairs if pairs else 0.0), float(grp.mean())


def main():
    call_id = sys.argv[1]
    token = E.api('engineToken')['token']
    vid, aud = find_pair(call_id, token)
    d = tempfile.mkdtemp(prefix='spkcheck-')
    try:
        print('call %s' % call_id)
        print('  video : %s (%.0f MB)' % (vid['name'], int(vid.get('size', 0)) / 1e6))
        print('  audio : %s (%.0f MB)' % (aud['name'], int(aud.get('size', 0)) / 1e6))
        E.TOKEN.t, E.TOKEN.at = token, __import__('time').time()
        v_path = E.drive_download(vid['id'], os.path.join(d, 'video.bin'))
        a_path = E.drive_download(aud['id'], os.path.join(d, 'call.bin'))

        offset, conf, detail = E.sync_offset(v_path, a_path, d)
        print('  sync  : offset %.3f confidence %.1f (%s)' % (offset, conf, detail))

        import scipy.io.wavfile as wav
        _, v = wav.read(os.path.join(d, 'v.wav'))
        _, a = wav.read(os.path.join(d, 'a.wav'))
        v, a = v.astype(np.float64), a.astype(np.float64)
        SR, n = 8000, int(HOP * 8000)

        vals, times = [], []
        floor = np.sqrt(np.mean(a ** 2)) * 0.35          # only frames where someone is actually talking
        for i in range(0, len(a) - n, n):
            t = i / SR
            j = int((t + offset) * SR)
            if j < 0 or j + n > len(v):
                continue
            arms = float(np.sqrt(np.mean(a[i:i + n] ** 2)))
            if arms < floor:
                continue
            vrms = float(np.sqrt(np.mean(v[j:j + n] ** 2))) + 1e-6
            vals.append(np.log(vrms / (arms + 1e-6)))
            times.append(t)
        x, times = np.asarray(vals), np.asarray(times)
        print('  voiced frames: %d of %d (%.0f%% of the call)' % (len(x), len(a) // n, 100.0 * len(x) * HOP / (len(a) / SR)))
        if len(x) < 40:
            raise SystemExit('too few voiced frames to judge')

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

        sep, runs, share = stats(x, grp, times)
        print('\n  SEPARATION     %.2f   (engine.py gate: %.2f)' % (sep, E.SPK_MIN_SEP))
        print('  TURN COHERENCE %.0f%%   (engine.py gate: %.0f%%)' % (runs * 100, E.SPK_MIN_RUNS * 100))
        print('  LOUDER SIDE    %.0f%% of voiced frames' % (share * 100))

        print('\n  distribution of log(room mic / call recording):')
        edges = np.linspace(x.min(), x.max(), 25)
        hist, _ = np.histogram(x, bins=edges)
        for k in range(len(hist)):
            print('   %7.2f %s %d' % (edges[k], '#' * int(50 * hist[k] / max(hist.max(), 1)), hist[k]))

        # contiguous turns, so the split can be eyeballed against what was said when
        print('\n  first 25 turns (loud side = probably the agent):')
        turns, cur = [], [times[0], times[0], grp[0]]
        for i in range(1, len(times)):
            if grp[i] == cur[2] and times[i] - cur[1] <= 1.5:
                cur[1] = times[i]
            else:
                turns.append(cur)
                cur = [times[i], times[i], grp[i]]
        turns.append(cur)
        turns = [t for t in turns if t[1] - t[0] >= 1.0]
        for t in turns[:25]:
            print('   %7.1f - %7.1f s  %s' % (t[0], t[1], 'AGENT' if t[2] else 'client'))
        print('\n  %d turns over 1s; median length %.1fs' %
              (len(turns), float(np.median([t[1] - t[0] for t in turns])) if turns else 0))
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    main()
