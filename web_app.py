from flask import Flask, render_template, request, redirect, url_for, send_file, abort, jsonify
from pathlib import Path
from threading import Thread
import uuid
import time
import shutil
import os
from dotenv import load_dotenv

from src.coordinator import Coordinator
from src.utils import setup_logging, get_spotify_creds, read_links, parse_errors
import re
from spotipy.oauth2 import SpotifyClientCredentials
import spotipy

app = Flask(__name__)

# In-memory run tracking: run_id -> {thread, output_dir, exit_code}
runs = {}

BASE_OUTPUT = Path("downloads")
BASE_OUTPUT.mkdir(exist_ok=True)
LOG_FILE = BASE_OUTPUT / "spotdl.log"
INPUTS_DIR = BASE_OUTPUT / ".inputs"
INPUTS_DIR.mkdir(exist_ok=True)


def run_coordinator(run_id: str, input_file: Path, output_dir: Path, logger):
    try:
        client_id, client_secret = get_spotify_creds(logger)
    except Exception as e:
        logger.error(f"Missing Spotify credentials: {e}")
        runs[run_id]["exit_code"] = -1
        return

    coord = Coordinator(output_dir, logger, client_id, client_secret)
    exit_code = coord.process_all(input_file)
    runs[run_id]["exit_code"] = exit_code


def list_download_folders():
    """Return list of (name, mtime) for directories under BASE_OUTPUT sorted by mtime desc.
    Exclude hidden and helper directories (names starting with '.' or 'run-')."""
    entries = []
    for p in BASE_OUTPUT.iterdir():
        if not p.is_dir():
            continue
        name = p.name
        if name.startswith('.') or name.startswith('run-'):
            continue
        entries.append((name, p.stat().st_mtime))
    entries.sort(key=lambda x: x[1], reverse=True)
    return entries


@app.route("/", methods=["GET"]) 
def index():
    # Build a friendly active-runs dict for the template with the fields the template expects
    active = {}
    for rid, meta in runs.items():
        out_dir = meta.get("output_dir")
        name = None
        if meta.get("playlist_name"):
            name = meta.get("playlist_name")
        elif meta.get("playlist_url"):
            name = meta.get("playlist_url")
        elif out_dir:
            try:
                name = out_dir.name
            except Exception:
                name = rid
        else:
            name = rid

        active[rid] = {
            "playlist_name": meta.get("playlist_name"),
            "playlist_url": meta.get("playlist_url"),
            "output_dir": out_dir,
            "running": bool(meta.get("thread") and meta.get("thread").is_alive()),
            "exit_code": meta.get("exit_code"),
            "name": name
        }

    return render_template("index.html", runs=active)


@app.route("/start", methods=["POST"])
def start():
    # Accept multiple uploaded files or pasted links
    uploaded_files = request.files.getlist("links_file")
    pasted = request.form.get("pasted_links", "").strip()

    if not any(f and f.filename for f in uploaded_files) and not pasted:
        return "Please upload at least one file or paste links.", 400

    created_runs = []
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    # Handle uploaded files (one run per file). Files are saved into downloads/.inputs to avoid creating run- folders
    for f in uploaded_files:
        if not f or not f.filename:
            continue
        run_id = uuid.uuid4().hex
        input_path = INPUTS_DIR / f"input-{run_id}.txt"
        f.save(str(input_path))
        output_dir = BASE_OUTPUT  # write downloads directly into downloads/{playlist folders}
        logger = setup_logging(output_dir)

        # Extract playlist info (first spotify playlist if present)
        links = read_links(input_path, logger)
        playlist_url = None
        playlist_name = None
        expected = None
        if links.get('spotify'):
            playlist_url = links['spotify'][0]
            # try to fetch playlist name + total
            try:
                client_id, client_secret = get_spotify_creds(logger)
                mgr = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
                sp = spotipy.Spotify(client_credentials_manager=mgr)
                m = re.search(r'playlist/([A-Za-z0-9]+)', playlist_url)
                if m:
                    pid = m.group(1)
                    pl = sp.playlist(pid)
                    playlist_name = pl.get('name')
                    expected = pl.get('tracks', {}).get('total')
            except Exception:
                playlist_name = None
                expected = None

        runs[run_id] = {"thread": None, "output_dir": output_dir, "exit_code": None, "playlist_url": playlist_url, "playlist_name": playlist_name, "expected": expected}
        t = Thread(target=run_coordinator, args=(run_id, input_path, output_dir, logger), daemon=True)
        runs[run_id]["thread"] = t
        t.start()
        created_runs.append(run_id)

    # Handle pasted links as a single run (saved into .inputs)
    if pasted:
        run_id = uuid.uuid4().hex
        input_path = INPUTS_DIR / f"input-{run_id}.txt"
        input_path.write_text(pasted, encoding="utf-8")
        output_dir = BASE_OUTPUT
        logger = setup_logging(output_dir)

        links = read_links(input_path, logger)
        playlist_url = None
        playlist_name = None
        expected = None
        if links.get('spotify'):
            playlist_url = links['spotify'][0]
            try:
                client_id, client_secret = get_spotify_creds(logger)
                mgr = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
                sp = spotipy.Spotify(client_credentials_manager=mgr)
                m = re.search(r'playlist/([A-Za-z0-9]+)', playlist_url)
                if m:
                    pid = m.group(1)
                    pl = sp.playlist(pid)
                    playlist_name = pl.get('name')
                    expected = pl.get('tracks', {}).get('total')
            except Exception:
                playlist_name = None
                expected = None

        runs[run_id] = {"thread": None, "output_dir": output_dir, "exit_code": None, "playlist_url": playlist_url, "playlist_name": playlist_name, "expected": expected}
        t = Thread(target=run_coordinator, args=(run_id, input_path, output_dir, logger), daemon=True)
        runs[run_id]["thread"] = t
        t.start()
        created_runs.append(run_id)

    # Redirect back to index and optionally select the first created run
    if created_runs:
        return redirect(url_for("index") + f"#run-{created_runs[0]}")
    return redirect(url_for("index"))


@app.route("/logs/<run_or_folder>", methods=["GET"])
def logs(run_or_folder):
    """Return JSON with running, exit_code, logs for either an active run id or an existing folder name under downloads."""
    # First try active runs
    meta = runs.get(run_or_folder)
    output_dir = None
    if meta:
        output_dir = meta["output_dir"]
        running = meta["thread"].is_alive()
        exit_code = meta.get("exit_code")
    else:
        # sanitize and check folder exists under BASE_OUTPUT
        candidate = BASE_OUTPUT / Path(run_or_folder).name
        if candidate.exists() and candidate.is_dir():
            # For folder views, show the global downloads/ spotdl.log (combined)
            output_dir = BASE_OUTPUT
            running = False
            exit_code = None
        else:
            return jsonify({"error": "Not found"}), 404

    # Use the global log file in downloads/ so subprocess output is visible
    log_file = BASE_OUTPUT / "spotdl.log"
    logs_text = ""
    if log_file.exists():
        try:
            with log_file.open("r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                start = max(0, size - 8000)
                f.seek(start)
                logs_text = f.read()
        except Exception:
            logs_text = "(Unable to read log file)"
    else:
        logs_text = "(No log file yet)"

    return jsonify({"running": running, "exit_code": exit_code, "logs": logs_text})


@app.route('/status_json/<run_id>', methods=['GET'])
def status_json(run_id):
    meta = runs.get(run_id)
    if not meta:
        return jsonify({'error': 'not found'}), 404

    playlist_url = meta.get('playlist_url')
    playlist_name = meta.get('playlist_name')
    expected = meta.get('expected')

    # Try to find playlist folder under downloads that matches playlist_name or playlist id
    found_folder = None
    if playlist_name:
        for p in BASE_OUTPUT.iterdir():
            if p.is_dir() and playlist_name.lower() in p.name.lower():
                found_folder = p
                break
    # fallback: try to find by id in playlist_url
    if not found_folder and playlist_url:
        m = re.search(r'playlist/([A-Za-z0-9]+)', playlist_url)
        if m:
            pid = m.group(1)
            for p in BASE_OUTPUT.iterdir():
                if p.is_dir() and pid in p.name:
                    found_folder = p
                    break

    songs = []
    downloaded = 0
    if found_folder:
        for f in sorted(found_folder.iterdir()):
            if f.is_file() and f.suffix.lower() in ['.mp3', '.m4a', '.flac']:
                downloaded += 1
                songs.append({'name': f.name, 'status': 'done'})

    # collect failed songs from errors
    errors_dir = BASE_OUTPUT / '.errors'
    failed_titles = set()
    if errors_dir.exists():
        for ef in errors_dir.iterdir():
            if ef.is_file():
                fail_list = parse_errors(ef, setup_logging(BASE_OUTPUT), playlist_url)
                for s in fail_list:
                    if s.title:
                        failed_titles.add(s.title)

    # mark failed songs in list (by matching title substring)
    for s in songs:
        for ft in failed_titles:
            if ft.lower() in s['name'].lower():
                s['status'] = 'failed'

    # If expected is None, set to downloaded to avoid showing 0/0
    expected_val = expected if expected is not None else downloaded

    return jsonify({'playlist_url': playlist_url, 'playlist_name': playlist_name, 'expected': expected_val, 'downloaded': downloaded, 'songs': songs})


@app.route("/open/<folder_name>", methods=["GET"])
def open_folder(folder_name):
    # Only allow opening folders under BASE_OUTPUT
    candidate = BASE_OUTPUT / Path(folder_name).name
    if not candidate.exists() or not candidate.is_dir():
        return "Folder not found", 404
    try:
        # Windows specific: open in Explorer
        if os.name == "nt":
            os.startfile(str(candidate))
        else:
            # Try xdg-open on Unix
            import subprocess
            subprocess.Popen(["xdg-open", str(candidate)])
        return redirect(url_for("index"))
    except Exception as e:
        return f"Failed to open folder: {e}", 500


@app.route("/download_folder/<folder_name>", methods=["GET"])
def download_folder(folder_name):
    candidate = BASE_OUTPUT / Path(folder_name).name
    if not candidate.exists() or not candidate.is_dir():
        return "Folder not found", 404
    archive = str(candidate) + ".zip"
    shutil.make_archive(str(candidate), "zip", root_dir=str(candidate))
    return send_file(archive, as_attachment=True, download_name=f"{candidate.name}.zip")


if __name__ == "__main__":
    load_dotenv()
    # Reduce noisy HTTP access logs (the UI polls /logs frequently)
    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=5000, debug=True)
