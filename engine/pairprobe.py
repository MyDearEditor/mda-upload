#!/usr/bin/env python3
"""Do an uploaded video and call recording actually contain the same conversation?

When sync fails, the engine can only say the two would not line up - it cannot say whether that is
because the files are from different calls or because there is too little shared material to be sure.
This transcribes the call recording, and the stretch of the video where the sync put its best guess,
so the words can simply be read side by side.

  set -a; . /opt/mda/.env; set +a
  /opt/mda/venv/bin/python3 -I /opt/mda/repo/engine/pairprobe.py <call_id> [win_start] [win_len]
"""
import os, sys, subprocess, tempfile, shutil, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E                                   # noqa: E402  (needs MDA_* env)
import spkcheck                                      # noqa: E402  (find_pair)


def say(txt, audio, t0, t1):
    """Transcribe a stretch and print it as one block of text."""
    from faster_whisper import WhisperModel
    import numpy as np
    m = WhisperModel(E.WHISPER, device='cpu', compute_type='int8')
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '%.3f' % t0, '-t', '%.3f' % (t1 - t0),
                          '-i', audio, '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'],
                         capture_output=True, check=True).stdout
    arr = np.frombuffer(raw, np.float32)
    out, _ = m.transcribe(arr, language='en', beam_size=5, vad_filter=False)
    words = ' '.join(s.text.strip() for s in out).strip()
    print('\n--- %s  (%s to %s of that file) ---' % (txt, E.mmss(t0), E.mmss(t1)))
    print(words if words else '(nothing audible)')
    return words


def main():
    call_id = sys.argv[1]
    win0 = float(sys.argv[2]) if len(sys.argv) > 2 else 440.0
    winl = float(sys.argv[3]) if len(sys.argv) > 3 else 160.0
    token = E.api('engineToken')['token']
    vid, aud = spkcheck.find_pair(call_id, token)
    d = tempfile.mkdtemp(prefix='pairprobe-')
    try:
        E.TOKEN.t, E.TOKEN.at = token, time.time()
        print('call %s' % call_id)
        v = E.drive_download(vid['id'], os.path.join(d, 'video.bin'))
        a = E.drive_download(aud['id'], os.path.join(d, 'call.bin'))
        vdur, adur = E.media_duration(v), E.media_duration(a)
        print('  video     : %-28s %s  (%.1f s)' % (vid['name'], E.mmss(vdur), vdur))
        print('  recording : %-28s %s  (%.1f s)' % (aud['name'], E.mmss(adur), adur))

        say('THE CALL RECORDING, in full', a, 0.0, adur)
        hi = min(vdur, win0 + winl)
        if win0 < vdur:
            say('THE VIDEO around the sync guess', v, win0, hi)
        else:
            print('\n(the video is shorter than the requested window, nothing to compare)')
        print('\nRead the two blocks above: same words = the pair overlaps and the sync is at fault;')
        print('different conversations = the agent uploaded files from two different calls.')
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    main()
