"""
KissKH Downloader - Web UI for PythonAnywhere
============================================
Deploy on PythonAnywhere (paid plan recommended for outbound requests).

How downloads work
------------------
1. User clicks Download → server creates an isolated temp folder per session.
2. A per-session config YAML is written that points kisskh-dl.py at that
   temp folder, so the file lands exactly where we expect it.
3. Progress streams to the browser via SSE.
4. When done, a "Save to My Computer" button appears.
5. Clicking it hits /api/serve/<session_id> which streams the file to the
   browser as a normal file-download attachment, then deletes the temp folder.
6. Nothing is kept permanently on the server.

Note: a threading lock serialises downloads so the temp YAML swap is safe.
For higher concurrency, swap the YAML swap for a proper --config CLI flag
if kisskh-dl.py ever gains one.
"""

import os
import sys
import re
import json
import queue
import shutil
import threading
import subprocess
import tempfile
from flask import Flask, request, jsonify, Response, render_template_string

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_CFG = os.path.join(BASE_DIR, 'config_kisskh.yaml')

sys.path.insert(0, BASE_DIR)

# ── State stores ─────────────────────────────────────────────────────────────
_download_queues:    dict[str, queue.Queue] = {}
_completed_downloads: dict[str, dict]       = {}

# Serialise downloads so the YAML swap is always consistent.
# For a personal-use tool this is fine; typically only one person downloads
# at a time anyway.
_download_lock = threading.Lock()

VIDEO_EXTS = {'.mp4', '.mkv', '.ts', '.m4v', '.avi', '.mov'}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_client():
    """Lazy-load KissKhClient from the original YAML config."""
    from Clients.KissKhClient import KissKhClient
    import yaml
    with open(ORIG_CFG) as f:
        full_cfg = yaml.safe_load(f)
    cfg = list(full_cfg.values())[0]
    cfg.setdefault('base_url', 'https://kisskh.ovh/')
    return KissKhClient(cfg)


def _parse_url(url: str):
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    return qs.get('id', [None])[0], qs.get('ep', [None])[0]


def _make_session_yaml(temp_dir: str) -> str:
    """
    Copy config_kisskh.yaml into temp_dir, overriding every download_dir
    to point at temp_dir.  Returns path to the new YAML file.
    """
    import yaml
    with open(ORIG_CFG) as f:
        cfg = yaml.safe_load(f) or {}

    for section in cfg.values():
        if isinstance(section, dict):
            if 'download_dir' in section:
                section['download_dir'] = temp_dir
            if 'temp_download_dir' in section:
                section['temp_download_dir'] = 'auto'

    session_yaml = os.path.join(temp_dir, 'config_session.yaml')
    with open(session_yaml, 'w') as f:
        yaml.dump(cfg, f)
    return session_yaml


def _find_video(directory: str) -> str | None:
    """Return the first video file found under directory."""
    for root, _, files in os.walk(directory):
        for name in files:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                return os.path.join(root, name)
    return None


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route('/api/fetch', methods=['POST'])
def api_fetch():
    data = request.get_json(force=True)
    url  = (data.get('url') or '').strip()
    if not url:
        return jsonify(error='URL is required'), 400
    try:
        client   = _load_client()
        drama_id, _ = _parse_url(url)
        if not drama_id:
            return jsonify(error='Could not parse drama ID from URL'), 400

        series_data = client._send_request(
            client.series_url + drama_id, return_type='json'
        )
        target = {
            'title':         series_data['title'],
            'series_id':     drama_id,
            'country':       series_data.get('country', ''),
            'episodesCount': series_data.get('episodesCount', 0),
            'series_type':   series_data.get('type', ''),
            'status':        series_data.get('status', ''),
            'episodes':      series_data['episodes'],
        }
        try:
            target['year'] = series_data['releaseDate'].split('-')[0]
        except Exception:
            target['year'] = ''

        episodes = client.fetch_episodes_list(target)
        return jsonify(
            title=target['title'],
            year=target['year'],
            country=target['country'],
            status=target['status'],
            total=len(episodes),
            episodes=[{
                'episode':     e['episode'],
                'episodeName': e['episodeName'],
                'episodeId':   e['episodeId'],
            } for e in episodes]
        )
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route('/api/download', methods=['POST'])
def api_download():
    """
    Start a download session.  Returns a session_id immediately.
    The actual download runs in a background thread.
    Poll /api/progress/<session_id> (SSE) for live log output.
    When the 'ready' event fires, call /api/serve/<session_id> to get the file.
    """
    data     = request.get_json(force=True)
    url      = (data.get('url') or '').strip()
    ep_start = data.get('ep_start', 1)
    ep_end   = data.get('ep_end', 1)
    quality  = data.get('quality', '1080')

    if not url:
        return jsonify(error='URL is required'), 400

    session_id = f"dl_{id(object())}_{ep_start}_{ep_end}"
    q = queue.Queue()
    _download_queues[session_id] = q

    def run():
        # Per-session isolated temp folder — kisskh-dl.py will write here.
        temp_dir = tempfile.mkdtemp(prefix=f'kisskh_{session_id}_')
        inputs   = f"{ep_start}-{ep_end}\n{quality}\ny\n"

        try:
            # Build a modified YAML that points download_dir at temp_dir.
            session_yaml = _make_session_yaml(temp_dir)
            q.put(('log', f'ℹ Session folder: {temp_dir}'))

            # Swap in the session YAML for the duration of this download.
            # The lock ensures no two downloads race on the YAML file.
            with _download_lock:
                orig_backup = ORIG_CFG + '.bak'
                shutil.copy2(ORIG_CFG, orig_backup)
                shutil.copy2(session_yaml, ORIG_CFG)
                try:
                    proc = subprocess.Popen(
                        [sys.executable, os.path.join(BASE_DIR, 'kisskh-dl.py'), url],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        cwd=BASE_DIR,
                        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                    )
                    try:
                        proc.stdin.write(inputs)
                        proc.stdin.flush()
                        proc.stdin.close()
                    except Exception:
                        pass

                    for line in proc.stdout:
                        q.put(('log', line.rstrip()))
                    proc.wait()
                    exit_code = proc.returncode
                finally:
                    # Always restore the original YAML even if something crashes.
                    shutil.copy2(orig_backup, ORIG_CFG)
                    os.remove(orig_backup)

            if exit_code == 0:
                filepath = _find_video(temp_dir)
                if filepath:
                    q.put(('log', f'ℹ File ready: {os.path.basename(filepath)}'))
                    _completed_downloads[session_id] = {
                        'filepath': filepath,
                        'filename': os.path.basename(filepath),
                        'temp_dir': temp_dir,
                    }
                    q.put(('ready', session_id))
                    q.put(('done',  'File ready — click Save to My Computer'))
                else:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    q.put(('done', 'Download finished but no video file was created'))
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)
                q.put(('done', f'Process exited with code {exit_code}'))

        except Exception as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            q.put(('error', str(exc)))
            q.put(('done', 'Failed — see error above'))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(session_id=session_id)


@app.route('/api/progress/<session_id>')
def api_progress(session_id):
    """SSE — streams live log lines from the download subprocess."""
    q = _download_queues.get(session_id)
    if q is None:
        return jsonify(error='Unknown session'), 404

    def generate():
        while True:
            try:
                kind, msg = q.get(timeout=60)
                yield f"data: {json.dumps({'type': kind, 'msg': msg})}\n\n"
                if kind == 'done':
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/serve/<session_id>')
def api_serve(session_id):
    """
    Stream the completed video file to the browser as a download attachment,
    then delete the temp folder from the server.
    This is the endpoint that triggers the browser's native download popup.
    """
    info = _completed_downloads.get(session_id)
    if not info:
        return jsonify(error='No completed download for this session'), 404

    filepath = info['filepath']
    filename = info['filename']
    temp_dir = info['temp_dir']

    if not os.path.exists(filepath):
        _completed_downloads.pop(session_id, None)
        return jsonify(error='File no longer on server'), 410

    ext  = os.path.splitext(filename)[1].lower()
    mime = {
        '.mp4': 'video/mp4', '.mkv': 'video/x-matroska',
        '.ts':  'video/mp2t', '.m4v': 'video/mp4',
        '.avi': 'video/x-msvideo', '.mov': 'video/quicktime',
    }.get(ext, 'application/octet-stream')

    size = os.path.getsize(filepath)

    def stream_then_delete():
        try:
            with open(filepath, 'rb') as fh:
                while True:
                    chunk = fh.read(512 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            _completed_downloads.pop(session_id, None)
            _download_queues.pop(session_id, None)

    return Response(
        stream_then_delete(),
        mimetype=mime,
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length':      str(size),
            'Accept-Ranges':       'none',
        },
    )


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>KissKH Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #0a0a0c;
    --surface:  #111114;
    --card:     #18181d;
    --border:   #2a2a32;
    --gold:     #c9a84c;
    --gold-dim: #7a6230;
    --text:     #e8e4dc;
    --muted:    #6b6878;
    --green:    #4caf82;
    --red:      #e05252;
    --blue:     #5b9cf6;
  }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 15px;
    line-height: 1.6;
  }

  body::before {
    content: '';
    position: fixed; inset: 0; z-index: 0; pointer-events: none;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
    background-size: 180px; opacity: .5;
  }

  .layout {
    position: relative; z-index: 1;
    min-height: 100vh;
    display: grid;
    grid-template-columns: 380px 1fr;
    grid-template-rows: auto 1fr;
  }

  header {
    grid-column: 1 / -1;
    padding: 28px 40px 24px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px;
  }
  .logo-mark {
    width: 38px; height: 38px; border-radius: 8px;
    background: linear-gradient(135deg, var(--gold) 0%, #7a5a1a 100%);
    display: flex; align-items: center; justify-content: center;
    font-family: 'Cormorant Garamond', serif;
    font-size: 22px; font-weight: 700; color: #0a0a0c; flex-shrink: 0;
  }
  .logo-text h1 {
    font-family: 'Cormorant Garamond', serif;
    font-size: 22px; font-weight: 600; letter-spacing: .02em; color: var(--text);
  }
  .logo-text p { font-size: 12px; color: var(--muted); margin-top: 1px; }
  .header-badge {
    margin-left: auto;
    background: #1a1a0e; border: 1px solid var(--gold-dim);
    color: var(--gold); font-size: 11px; padding: 3px 10px; border-radius: 20px;
    letter-spacing: .06em; text-transform: uppercase;
  }

  .panel-left {
    border-right: 1px solid var(--border);
    padding: 32px 28px;
    display: flex; flex-direction: column; gap: 24px;
    overflow-y: auto;
  }

  .section-label {
    font-size: 10px; letter-spacing: .12em; text-transform: uppercase;
    color: var(--gold); margin-bottom: 10px; display: block;
  }

  .input-group { display: flex; flex-direction: column; gap: 6px; }
  .input-group label { font-size: 12px; color: var(--muted); }
  input[type=text], input[type=number], select {
    width: 100%;
    background: var(--card); border: 1px solid var(--border);
    color: var(--text); padding: 10px 14px; border-radius: 8px;
    font-family: inherit; font-size: 14px;
    outline: none; transition: border-color .2s;
  }
  input[type=text]:focus, input[type=number]:focus, select:focus { border-color: var(--gold-dim); }
  input::placeholder { color: var(--muted); }

  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

  .btn {
    width: 100%; padding: 12px; border-radius: 8px; border: none;
    font-family: inherit; font-size: 14px; font-weight: 500;
    cursor: pointer; transition: all .2s; letter-spacing: .01em;
  }
  .btn-gold { background: linear-gradient(135deg, #c9a84c, #9a7530); color: #0a0a0c; }
  .btn-gold:hover { filter: brightness(1.1); transform: translateY(-1px); }
  .btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
  .btn-ghost:hover { border-color: var(--gold-dim); color: var(--gold); }

  /* The "save to computer" button — highlighted in blue so it stands out */
  .btn-download {
    background: linear-gradient(135deg, #3a7bd5, #1e4fa8);
    color: #fff;
    display: none;
    animation: popIn .35s cubic-bezier(.34,1.56,.64,1);
  }
  .btn-download.visible { display: block; }
  .btn-download:hover { filter: brightness(1.12); transform: translateY(-1px); }
  @keyframes popIn {
    from { transform: scale(.85); opacity: 0; }
    to   { transform: scale(1);   opacity: 1; }
  }

  .btn:disabled { opacity: .4; cursor: not-allowed; transform: none !important; }

  .drama-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px; display: none;
  }
  .drama-card.visible { display: block; }
  .drama-title {
    font-family: 'Cormorant Garamond', serif;
    font-size: 20px; font-weight: 600; line-height: 1.2; color: var(--text); margin-bottom: 6px;
  }
  .drama-meta { font-size: 12px; color: var(--muted); }
  .drama-meta span + span::before { content: ' · '; }
  .ep-badge {
    display: inline-block; margin-top: 8px;
    background: #1a1a0e; border: 1px solid var(--gold-dim);
    color: var(--gold); font-size: 11px; padding: 2px 8px; border-radius: 20px;
  }

  .ep-list {
    max-height: 180px; overflow-y: auto;
    display: flex; flex-wrap: wrap; gap: 6px; padding: 2px;
  }
  .ep-chip {
    background: var(--surface); border: 1px solid var(--border);
    color: var(--muted); font-size: 12px; padding: 4px 10px; border-radius: 20px;
    cursor: pointer; transition: all .15s; user-select: none;
  }
  .ep-chip:hover { border-color: var(--gold-dim); color: var(--gold); }
  .ep-chip.selected { background: #1a1a0e; border-color: var(--gold); color: var(--gold); }

  .panel-right {
    padding: 32px 36px;
    display: flex; flex-direction: column; gap: 24px; overflow-y: auto;
  }

  .terminal {
    flex: 1;
    background: #0d0d10; border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 20px; overflow-y: auto;
    font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 13px;
    min-height: 400px; max-height: 560px;
    scrollbar-width: thin; scrollbar-color: var(--border) transparent;
  }
  .terminal-header {
    display: flex; align-items: center; gap: 8px; margin-bottom: 14px;
    padding-bottom: 12px; border-bottom: 1px solid var(--border);
  }
  .dot { width: 10px; height: 10px; border-radius: 50%; }
  .dot-r { background: #e05252; } .dot-y { background: #c9a84c; } .dot-g { background: #4caf82; }
  .term-title { margin-left: 6px; font-size: 12px; color: var(--muted); }

  .log-line { line-height: 1.7; }
  .log-line.info    { color: #a0d4b5; }
  .log-line.warn    { color: #e0c070; }
  .log-line.error   { color: var(--red); }
  .log-line.success { color: var(--green); }
  .log-line.muted   { color: var(--muted); }
  .log-line.bold    { color: var(--text); font-weight: 500; }
  .log-line.ready   { color: var(--blue); font-weight: 500; }

  .status-bar {
    display: flex; align-items: center; gap: 12px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px;
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--muted); flex-shrink: 0; transition: background .3s;
  }
  .status-dot.active { background: var(--green); animation: pulse 1.2s infinite; }
  .status-dot.done   { background: var(--green); }
  .status-dot.error  { background: var(--red); }
  .status-dot.ready  { background: var(--blue); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  .status-text { font-size: 13px; color: var(--muted); flex: 1; }
  .status-text.active, .status-text.ready { color: var(--text); }

  /* Info banner explaining the flow */
  .info-banner {
    background: #0e1520; border: 1px solid #2a3a55;
    border-radius: 8px; padding: 10px 14px;
    font-size: 12px; color: #7aa4d4; line-height: 1.5;
  }
  .info-banner strong { color: #a0c4f0; }

  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

  .empty-state {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; height: 100%; gap: 12px;
    color: var(--muted); text-align: center;
  }
  .empty-icon { font-size: 40px; opacity: .3; }
  .empty-state p { font-size: 13px; max-width: 240px; }
</style>
</head>
<body>
<div class="layout">

  <header>
    <div class="logo-mark">K</div>
    <div class="logo-text">
      <h1>KissKH Downloader</h1>
      <p>Web-hosted download manager</p>
    </div>
    <div class="header-badge">Personal Use Only</div>
  </header>

  <aside class="panel-left">
    <div>
      <span class="section-label">Source</span>
      <div class="input-group">
        <label>KissKH Episode URL</label>
        <input type="text" id="urlInput" placeholder="https://kisskh.do/Drama/..."/>
      </div>
      <br/>
      <button class="btn btn-ghost" id="fetchBtn" onclick="fetchEpisodes()">Fetch Episodes</button>
    </div>

    <div class="drama-card" id="dramaCard">
      <div class="drama-title" id="dramaTitle">—</div>
      <div class="drama-meta">
        <span id="dramaYear"></span>
        <span id="dramaCountry"></span>
        <span id="dramaStatus"></span>
      </div>
      <span class="ep-badge" id="epBadge"></span>
    </div>

    <div id="epSection" style="display:none">
      <span class="section-label">Episodes</span>
      <div id="epChips" class="ep-list"></div>
      <br/>
      <div class="row">
        <div class="input-group">
          <label>From</label>
          <input type="number" id="epStart" value="1" min="1"/>
        </div>
        <div class="input-group">
          <label>To</label>
          <input type="number" id="epEnd" value="1" min="1"/>
        </div>
      </div>
    </div>

    <div id="settingsSection" style="display:none">
      <span class="section-label">Settings</span>
      <div class="input-group">
        <label>Quality</label>
        <select id="quality">
          <option value="1080">1080p</option>
          <option value="720">720p</option>
          <option value="480">480p</option>
          <option value="360">360p</option>
        </select>
      </div>
    </div>

    <div style="margin-top:auto; display:flex; flex-direction:column; gap:10px;">
      <button class="btn btn-gold" id="dlBtn" onclick="startDownload()" disabled>
        ⬇ Download Episode(s)
      </button>
      <!--
        This button only appears after the server-side download is complete.
        Clicking it hits /api/serve/<session_id> which streams the file
        directly to the browser and deletes it from the server afterwards.
      -->
      <button class="btn btn-download" id="saveBtn" onclick="saveToComputer()">
        💾 Save to My Computer
      </button>
    </div>
  </aside>

  <main class="panel-right">
    <div>
      <span class="section-label">Download Log</span>
      <div class="info-banner">
        <strong>How it works:</strong> The server downloads the episode, then a
        <em>"Save to My Computer"</em> button appears — clicking it streams the
        file directly to your browser's download manager. Nothing is kept on the server.
      </div>
    </div>

    <div class="terminal" id="terminal">
      <div class="terminal-header">
        <div class="dot dot-r"></div>
        <div class="dot dot-y"></div>
        <div class="dot dot-g"></div>
        <span class="term-title">output</span>
      </div>
      <div id="logContainer">
        <div class="empty-state">
          <div class="empty-icon">⬇</div>
          <p>Paste a KissKH URL and click <strong>Fetch Episodes</strong> to begin</p>
        </div>
      </div>
    </div>

    <div class="status-bar">
      <div class="status-dot" id="statusDot"></div>
      <div class="status-text" id="statusText">Idle — waiting for input</div>
    </div>
  </main>

</div>

<script>
  let allEpisodes    = [];
  let selectedEps    = new Set();
  let currentSource  = null;
  let readySessionId = null;   // set when server says the file is ready

  // ── Fetch Episodes ────────────────────────────────────────────────────────
  async function fetchEpisodes() {
    const url = document.getElementById('urlInput').value.trim();
    if (!url) return alert('Please paste a KissKH URL first.');

    setStatus('active', 'Fetching episode list…');
    document.getElementById('fetchBtn').disabled = true;
    clearLog();
    addLog('Contacting KissKH…', 'muted');

    try {
      const res  = await fetch('/api/fetch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url})
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      currentSource = {url, ...data};
      allEpisodes   = data.episodes;

      document.getElementById('dramaTitle').textContent   = data.title;
      document.getElementById('dramaYear').textContent    = data.year;
      document.getElementById('dramaCountry').textContent = data.country;
      document.getElementById('dramaStatus').textContent  = data.status;
      document.getElementById('epBadge').textContent      = `${data.total} episodes`;
      document.getElementById('dramaCard').classList.add('visible');

      const chips = document.getElementById('epChips');
      chips.innerHTML = '';
      allEpisodes.forEach(ep => {
        const chip = document.createElement('div');
        chip.className   = 'ep-chip';
        chip.textContent = ep.episode;
        chip.dataset.ep  = ep.episode;
        chip.onclick     = () => toggleEp(chip, ep.episode);
        chips.appendChild(chip);
      });

      const nums = allEpisodes.map(e => parseFloat(e.episode));
      document.getElementById('epStart').value = Math.min(...nums);
      document.getElementById('epEnd').value   = Math.max(...nums);

      document.getElementById('epSection').style.display      = '';
      document.getElementById('settingsSection').style.display = '';
      document.getElementById('dlBtn').disabled = false;

      clearLog();
      addLog(`✓ Found "${data.title}" — ${data.total} episodes`, 'success');
      setStatus('', 'Ready — configure your download below');

    } catch(e) {
      addLog('✗ ' + e.message, 'error');
      setStatus('error', 'Failed to fetch episode list');
    } finally {
      document.getElementById('fetchBtn').disabled = false;
    }
  }

  function toggleEp(chip, ep) {
    if (selectedEps.has(ep)) {
      selectedEps.delete(ep);
      chip.classList.remove('selected');
    } else {
      selectedEps.add(ep);
      chip.classList.add('selected');
      const nums = [...selectedEps].map(Number).sort((a,b) => a-b);
      document.getElementById('epStart').value = nums[0];
      document.getElementById('epEnd').value   = nums[nums.length-1];
    }
  }

  // ── Start Download ────────────────────────────────────────────────────────
  async function startDownload() {
    if (!currentSource) return;

    const epStart = parseInt(document.getElementById('epStart').value);
    const epEnd   = parseInt(document.getElementById('epEnd').value);
    const quality = document.getElementById('quality').value;

    // Reset ready-download state
    readySessionId = null;
    hideSaveButton();

    document.getElementById('dlBtn').disabled = true;
    clearLog();
    setStatus('active', `Server is downloading episodes ${epStart}–${epEnd}…`);
    addLog(`▶ Requesting download: episodes ${epStart}–${epEnd} @ ${quality}p`, 'bold');
    addLog('The server will fetch and prepare your file. This may take a few minutes.', 'muted');
    addLog('', 'muted');

    try {
      const res  = await fetch('/api/download', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          url: currentSource.url,
          ep_start: epStart,
          ep_end:   epEnd,
          quality,
        })
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      // Stream server logs via SSE
      const evtSrc = new EventSource(`/api/progress/${data.session_id}`);

      evtSrc.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'ping')  return;
        if (msg.type === 'log')   addLog(msg.msg);

        if (msg.type === 'ready') {
          // Server has finished downloading; store session id for the save button
          readySessionId = msg.msg;
          showSaveButton();
        }

        if (msg.type === 'done') {
          addLog('', 'muted');
          addLog('✓ ' + msg.msg, 'success');
          if (readySessionId) {
            addLog('👆 Click "Save to My Computer" to download the file to your device.', 'ready');
            setStatus('ready', 'File ready — click Save to My Computer');
          } else {
            setStatus('done', msg.msg);
          }
          document.getElementById('dlBtn').disabled = false;
          evtSrc.close();
        }

        if (msg.type === 'error') {
          addLog('✗ ' + msg.msg, 'error');
          setStatus('error', 'Download failed');
          document.getElementById('dlBtn').disabled = false;
          evtSrc.close();
        }
      };

      evtSrc.onerror = () => {
        addLog('SSE connection lost — the download may still be running on the server.', 'warn');
        evtSrc.close();
        document.getElementById('dlBtn').disabled = false;
      };

    } catch(e) {
      addLog('✗ ' + e.message, 'error');
      setStatus('error', 'Download failed to start');
      document.getElementById('dlBtn').disabled = false;
    }
  }

  // ── Save to Computer ──────────────────────────────────────────────────────
  // Navigating to the /api/serve endpoint triggers a browser file download.
  // The server streams the file, then deletes it. No temp file remains.
  function saveToComputer() {
    if (!readySessionId) return;
    addLog('⬇ Initiating browser download…', 'info');
    // Using an <a> click with the serve URL triggers the browser's native
    // download dialog — same as clicking any download link on the web.
    const a = document.createElement('a');
    a.href  = `/api/serve/${readySessionId}`;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);

    hideSaveButton();
    setStatus('done', 'Download sent to your browser — check your downloads folder');
    addLog('✓ File is being saved to your browser downloads folder.', 'success');
    readySessionId = null;
  }

  function showSaveButton() {
    document.getElementById('saveBtn').classList.add('visible');
  }
  function hideSaveButton() {
    document.getElementById('saveBtn').classList.remove('visible');
  }

  // ── Log helpers ───────────────────────────────────────────────────────────
  function clearLog() {
    document.getElementById('logContainer').innerHTML = '';
  }

  function addLog(text, cls) {
    const container = document.getElementById('logContainer');
    const line = document.createElement('div');
    line.className  = 'log-line ' + (cls || classifyLine(text));
    line.textContent = text || '\u00a0';
    container.appendChild(line);
    const term = document.getElementById('terminal');
    term.scrollTop = term.scrollHeight;
  }

  function classifyLine(text) {
    if (!text) return 'muted';
    const t = text.toLowerCase();
    if (t.includes('error') || t.includes('failed') || t.includes('✗')) return 'error';
    if (t.includes('warn'))  return 'warn';
    if (t.includes('✓') || t.includes('done') || t.includes('complete')) return 'success';
    if (t.startsWith('▶') || t.includes('downloading')) return 'bold';
    if (t.includes('info') || t.includes('%') || t.includes('mb')) return 'info';
    return 'muted';
  }

  function setStatus(type, text) {
    const dot = document.getElementById('statusDot');
    const txt = document.getElementById('statusText');
    dot.className = 'status-dot ' + type;
    txt.className = 'status-text ' + type;
    txt.textContent = text;
  }

  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') fetchEpisodes();
  });
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)


if __name__ == '__main__':
    print("\n  ✦  KissKH Downloader UI")
    print("  ─────────────────────────")
    print("  Open:  http://localhost:5000\n")
    app.run(debug=False, port=5000, threaded=True)
