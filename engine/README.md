# MDA editing engine

Runs on a small Ubuntu server. Polls the Apps Script doorman every 2 minutes, edits each uploaded call into
Instagram Reels, re-checks compliance, and uploads the survivors to the agent's Drive folder.

## Files
- `engine.py` — the loop (download → sync → transcribe → Claude picks clips + redactions → render → compliance re-check → upload → report)
- `render.py` — renders one clip (captions, bleeps, cuts, dead-air trim, 1080x1920 H.264, -14 LUFS)
- `install.sh` — first-boot installer (used as the droplet's cloud-init user data)

## On the server
- `/opt/mda/.env` — settings and secrets (never committed)
- `bash /opt/mda/configure.sh` — paste the two secrets and start the service
- `journalctl -u mda-engine -f` — live log
- `bash /opt/mda/update.sh` — pull the latest code from this repo and restart

## Test locally
```
pip install faster-whisper numpy scipy anthropic requests
python3 -I render.py clip_config.json
```
