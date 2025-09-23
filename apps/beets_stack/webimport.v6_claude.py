#!/usr/bin/env python3
import os
import re
import subprocess
import threading
import time
import fcntl
import termios
import struct
import pty
import select
import json
from collections import defaultdict
from flask import Flask, render_template_string, request, redirect, url_for, jsonify

# --- Globale Konfiguration ---
app = Flask(__name__)
INPUT_DIR = "/input"
CONFIG_DIR = "/config"

class BeetsSession:
    def __init__(self):
        self.process = None
        self.master_fd = None
        self.output_buffer = []
        self.current_folder = None
        self.lock = threading.Lock()
        self.pending_editor_path = None
        
    def start_import(self, folder):
        """Startet einen neuen Import mit pseudo-terminal"""
        if self.process and self.process.poll() is None:
            return False
            
        self.output_buffer = []
        self.current_folder = folder
        full_path = os.path.join(INPUT_DIR, folder)
        
        # Environment setup
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = "120"
        env["LINES"] = "40"

        # Web-Editor-Helfer und EDITOR setzen
        helper_path = os.path.join(CONFIG_DIR, "web_editor.py")
        if not os.path.exists(helper_path):
            with open(helper_path, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env python3\n"
                    "import sys, os, time\n"
                    "p = sys.argv[-1]\n"
                    "print(f\"[[OPEN_YAML:{p}]]\")\n"
                    "done = p + '.done'\n"
                    "while not os.path.exists(done):\n"
                                        "    time.sleep(0.2)\n"
                    "try:\n"
                    "    os.remove(done)\n"
                    "except Exception:\n"
                    "    pass\n"
                )
            os.chmod(helper_path, 0o755)
        env["EDITOR"] = f"/usr/bin/env python3 {helper_path}"
        
        # Erstelle pseudo-terminal
        self.master_fd, slave_fd = pty.openpty()
        
        # Terminal-Größe
        winsize = struct.pack("HHHH", 40, 120, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        
        # Prozess starten (-t = timid)
        self.process = subprocess.Popen(
            ["beet", "import", "-t", full_path],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            preexec_fn=os.setsid
        )
        
        os.close(slave_fd)
        
        # non-blocking
        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        
        # Output-Reader
        threading.Thread(target=self._read_output, daemon=True).start()
        
        return True
    
    def _read_output(self):
        """Liest kontinuierlich Output vom PTY"""
        while self.process and self.process.poll() is None:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                if ready:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        text = data.decode('utf-8', errors='replace')

                        # Editor-Marker erkennen und puffern, Marker aus Output entfernen
                        m = re.findall(r'\[\[OPEN_YAML:(.*?)\]\]', text)
                        if m:
                            with self.lock:
                                self.pending_editor_path = m[-1]
                            text = re.sub(r'\[\[OPEN_YAML:.*?\]\]', '', text)

                        with self.lock:
                            self.output_buffer.append(text)
                            if len(self.output_buffer) > 500:
                                self.output_buffer = self.output_buffer[-400:]
            except OSError:
                break
                
        # Cleanup
        if self.master_fd:
            try:
                os.close(self.master_fd)
            except:
                pass
        self.master_fd = None
        self.current_folder = None
    
    def send_input(self, text):
        """Sendet Input an den Prozess"""
        if self.master_fd and self.process and self.process.poll() is None:
            try:
                data = (text + "\n").encode('utf-8')
                os.write(self.master_fd, data)
                return True
            except OSError:
                return False
        return False
    
    def get_output(self):
        """Gibt den aktuellen Output zurück"""
        with self.lock:
            return ''.join(self.output_buffer)
    
    def stop_import(self):
        """Stoppt den laufenden Import"""
        if self.process:
            try:
                os.write(self.master_fd, b'\x03')  # Ctrl+C
                time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.terminate()
                    time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.kill()
            except:
                pass
            self.process = None
        
        if self.master_fd:
            try:
                os.close(self.master_fd)
            except:
                pass
            self.master_fd = None
            
    def is_running(self):
        """Prüft ob ein Import läuft"""
        return self.process and self.process.poll() is None

# Globale Session
session = BeetsSession()

# --- Beets Library Functions ---
def get_library_stats():
    """Holt Statistiken aus der Bibliothek"""
    try:
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        result = subprocess.run(
            ["beet", "stats"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10
        )
        return result.stdout
    except Exception as e:
        return f"Fehler: {e}"

def get_library_items():
    """Holt Items aus der Beets-Bibliothek und gruppiert nach Artist"""
    try:
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        result = subprocess.run(
            ["beet", "ls", "-a", "-f", "$id||$albumartist||$album||$year||$genre||$path"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10
        )
        
        artists = defaultdict(list)
        for line in result.stdout.strip().split('\n'):
            if '||' in line:
                parts = line.split('||')
                if len(parts) >= 5:
                    artist = parts[1] or "Unknown Artist"
                    album_info = {
                        'id': parts[0],
                        'artist': parts[1],
                        'album': parts[2],
                        'year': parts[3],
                        'genre': parts[4] if len(parts) > 4 else '',
                        'path': parts[5] if len(parts) > 5 else ''
                    }
                    artists[artist].append(album_info)
        
        # Sortiere Artists alphabetisch und Alben nach Jahr
        sorted_artists = {}
        for artist in sorted(artists.keys()):
            sorted_artists[artist] = sorted(artists[artist], key=lambda x: x['year'] or '0')
        
        return sorted_artists
    except Exception as e:
        print(f"Error getting library items: {e}")
        return {}

def get_album_details(album_id):
    """Holt detaillierte Informationen über ein Album"""
    try:
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        result = subprocess.run(
            ["beet", "ls", "-a", f"id:{album_id}", "-f", 
             "$id||$albumartist||$album||$year||$genre||$label||$catalognum||$country||$albumtype||$mb_albumid||$path"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10
        )
        
        if result.stdout.strip():
                        parts = result.stdout.strip().split('||')
            details = {
                'id': parts[0] if len(parts) > 0 else '',
                'albumartist': parts[1] if len(parts) > 1 else '',
                'album': parts[2] if len(parts) > 2 else '',
                'year': parts[3] if len(parts) > 3 else '',
                'genre': parts[4] if len(parts) > 4 else '',
                'label': parts[5] if len(parts) > 5 else '',
                'catalognum': parts[6] if len(parts) > 6 else '',
                'country': parts[7] if len(parts) > 7 else '',
                'albumtype': parts[8] if len(parts) > 8 else '',
                'mb_albumid': parts[9] if len(parts) > 9 else '',
                'path': parts[10] if len(parts) > 10 else ''
            }
            
            # Hole Tracks
            track_result = subprocess.run(
                ["beet", "ls", f"album_id:{album_id}", "-f", "$track||$title||$length||$bitrate"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10
            )
            
            tracks = []
            for line in track_result.stdout.strip().split('\n'):
                if '||' in line:
                    track_parts = line.split('||')
                    if len(track_parts) >= 2:
                        tracks.append({
                            'track': track_parts[0],
                            'title': track_parts[1],
                            'length': track_parts[2] if len(track_parts) > 2 else '',
                            'bitrate': track_parts[3] if len(track_parts) > 3 else ''
                        })
            
            details['tracks'] = tracks
            return details
        return None
    except Exception as e:
        print(f"Error getting album details: {e}")
        return None

def delete_library_item(item_id):
    """Löscht ein Item aus der Bibliothek"""
    try:
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        subprocess.run(
            ["beet", "rm", "-a", f"id:{item_id}"],
            env=env,
            timeout=10,
            input="y\n",
            text=True
        )
        return True
    except Exception as e:
        print(f"Error deleting item: {e}")
        return False

def update_library():
    """Aktualisiert die Bibliothek"""
    try:
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        result = subprocess.run(
            ["beet", "update"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30
        )
        return result.stdout
    except Exception as e:
        return f"Fehler: {e}"

def move_library():
    """Verschiebt Library-Dateien gemäß Konfiguration"""
    try:
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        result = subprocess.run(
            ["beet", "move"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )
        return result.stdout
    except Exception as e:
        return f"Fehler: {e}"

# --- HTML Templates ---
TEMPLATE = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Beets Webimport</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root{
            --pad:16px;
            --gap:10px;
            --radius:8px;
            --font-mono:"Cascadia Code","SF Mono",Monaco,Consolas,monospace;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { height:100%; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a1a;
            color: #e0e0e0;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: #2a2a2a;
            padding: 12px var(--pad);
            border-bottom: 1px solid #444;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: var(--gap);
            flex-wrap: wrap;
        }
        h1 { font-size: clamp(16px, 2.2vw, 20px); color: #fff; }
        .controls { display: flex; gap: var(--gap); align-items: center; flex-wrap: wrap; }
        .btn {
            padding: 10px 14px;
            border: none;
            border-radius: var(--radius);
            cursor: pointer;
            font-size: 14px;
            transition: opacity 0.2s, transform .05s;
            touch-action: manipulation;
            text-decoration: none;
            display: inline-block;
        }
        .btn:active { transform: translateY(1px); }
        .btn:hover { opacity: 0.9; }
        .btn-primary { background: #007acc; color: white; }
        .btn-danger { background: #d32f2f; color: white; }
        .btn-success { background: #4caf50; color: white; }
        .btn-warning { background: #ff9800; color: white; }
        .btn-info { background: #00bcd4; color: white; }
        .btn-small { padding: 6px 10px; font-size: 12px; min-width: 36px; }
        .status {
            padding: 6px 10px;
            background: #333;
            border-radius: var(--radius);
            font-size: 12px;
            color: #aaa;
            max-width: 100%;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 0;
            overflow-y: auto;
        }

        /* Terminal */
        .terminal-container {
            flex: 1;
            background: #0c0c0c;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        #terminal {
            flex: 1;
            padding: 10px;
            overflow: auto;
            font-family: var(--font-mono);
            font-size: clamp(11px, 1.8vw, 13px);
            line-height: 1.4;
            white-space: pre-wrap;
            word-break: break-word;
            color: #00ff00;
        }

        /* ANSI color support */
        .ansi-black { color: #000; }
        .ansi-red { color: #cd3131; }
        .ansi-green { color: #0dbc79; }
        .ansi-yellow { color: #e5e510; }
        .ansi-blue { color: #2472c8; }
        .ansi-magenta { color: #bc3fbc; }
        .ansi-cyan { color: #11a8cd; }
        .ansi-white { color: #e5e5e5; }

        /* Shortcut buttons */
        .shortcuts {
            background: #1a1a1a;
            border-top: 1px solid #444;
            border-bottom: 1px solid #444;
            padding: 8px var(--pad);
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            justify-content: center;
        }
        .shortcut-btn {
            background: #2a2a2a;
            color: #e0e0e0;
            border: 1px solid #444;
            padding: 6px 12px;
            border-radius: var(--radius);
            cursor: pointer;
            font-size: 12px;
            font-family: var(--font-mono);
            transition: background 0.2s;
        }
        .shortcut-btn:hover { background: #333; }
        .shortcut-btn:active { background: #3a3a3a; }

        /* Input area */
        .input-area {
            background: #1a1a1a;
            border-top: 1px solid #444;
            padding: var(--pad);
        }
        .input-form {
            display: flex;
            gap: var(--gap);
            width: 100%;
        }
        #input {
            flex: 1;
            background: #0c0c0c;
            color: #00ff00;
            border: 1px solid #444;
            padding: 12px;
            font-family: var(--font-mono);
            font-size: 14px;
            border-radius: var(--radius);
            min-height: 44px;
        }
        #input:focus { outline: none; border-color: #007acc; }

        /* Folder selection */
        .folder-selection {
            padding: 24px var(--pad);
            width: 100%;
            max-width: 1400px;
            margin: 0 auto;
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            flex-wrap: wrap;
            gap: var(--gap);
        }
        .section-header h2 {
            font-size: clamp(16px, 2.4vw, 20px);
        }
        .folder-list {
            list-style: none;
            margin-top: 8px;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: var(--gap);
        }
        .folder-item {
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: var(--radius);
            padding: 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }
        .folder-item:hover { background: #333; }
        .folder-item span { 
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        /* Library view */
        .library-section {
            margin-top: 32px;
            padding-top: 24px;
            border-top: 1px solid #444;
        }
        
        .artist-group {
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: var(--radius);
            margin-bottom: 12px;
            overflow: hidden;
        }
        
        .artist-header {
            padding: 14px;
            background: #333;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            user-select: none;
        }
        
        .artist-header:hover {
            background: #3a3a3a;
        }
        
        .artist-name {
            font-weight: bold;
            font-size: 16px;
        }
        
        .artist-count {
            font-size: 12px;
            color: #aaa;
            padding: 4px 8px;
            background: #2a2a2a;
            border-radius: var(--radius);
        }
        
        .albums-container {
            display: none;
            padding: 8px;
        }
        
        .albums-container.show {
            display: block;
        }
        
        .album-item {
            background: #252525;
            border: 1px solid #333;
            border-radius: var(--radius);
            padding: 12px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
        }
        
        .album-item:hover {
            background: #2a2a2a;
        }
        
        .album-info {
            flex: 1;
            min-width: 0;
        }
        
        .album-title {
            font-weight: 500;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .album-meta {
            font-size: 12px;
            color: #aaa;
            margin-top: 4px;
        }
        
        .album-actions {
            display: flex;
            gap: 6px;
        }

        /* Modal */
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            overflow: auto;
        }
        
        .modal.show {
            display: block;
        }
        
        .modal-content {
            background: #2a2a2a;
            margin: 5% auto;
            padding: 0;
            border: 1px solid #444;
            border-radius: var(--radius);
            width: 90%;
            max-width: 800px;
            max-height: 80vh;
            display: flex;
            flex-direction: column;
        }
        
        .modal-header {
            padding: 16px;
            background: #333;
            border-bottom: 1px solid #444;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .modal-body {
            padding: 16px;
            overflow-y: auto;
            flex: 1;
        }
        
        .close {
            color: #aaa;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
        }
        
        .close:hover {
            color: #fff;
        }
        
        .detail-grid {
            display: grid;
            grid-template-columns: 150px 1fr;
            gap: 12px;
            margin-bottom: 20px;
        }
        
        .detail-label {
            font-weight: bold;
            color: #aaa;
        }
        
        .detail-value {
            color: #e0e0e0;
            word-break: break-word;
            overflow-wrap: break-word;
        }
        
        .track-list {
            margin-top: 20px;
        }
        
        .track-item {
            padding: 8px;
            background: #333;
            border-radius: var(--radius);
            margin-bottom: 6px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .track-info {
            flex: 1;
        }
        
        .track-number {
            display: inline-block;
            min-width: 30px;
            color: #aaa;
        }

        /* Scrollbar */
        #terminal::-webkit-scrollbar, .modal-body::-webkit-scrollbar { 
            width: 8px; 
            height: 8px; 
        }
        #terminal::-webkit-scrollbar-track, .modal-body::-webkit-scrollbar-track { 
            background: #1a1a1a; 
        }
        #terminal::-webkit-scrollbar-thumb, .modal-body::-webkit-scrollbar-thumb { 
            background: #444; 
            border-radius: 5px; 
        }
        #terminal::-webkit-scrollbar-thumb:hover, .modal-body::-webkit-scrollbar-thumb:hover { 
            background: #555; 
        }

        /* Mobile tweaks */
        @media (max-width: 700px) {
            .controls { width: 100%; }
            .status { flex: 1; }
            .input-form { flex-direction: column; }
            .btn { width: 100%; }
            .folder-item { flex-direction: column; align-items: stretch; }
            .folder-item .btn { width: 100%; }
            .album-item { flex-direction: column; align-items: stretch; }
            .album-actions { width: 100%; }
            .detail-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🎵 Beets Webimport</h1>
        {% if is_running %}
        <div class="controls">
            <span class="status">Import läuft: {{ current_folder }}</span>
            <button onclick="location.reload()" class="btn btn-primary">↻ Refresh</button>
            <a href="{{ url_for('abort') }}" class="btn btn-danger">✕ Abbrechen</a>
        </div>
        {% endif %}
    </div>
    
    <div class="main-content">
        {% if not is_running %}
        <div class="folder-selection">
            <div class="section-header">
                <h2>Wähle ein Hörbuch zum Import:</h2>
            </div>
            <ul class="folder-list">
                {% for folder in folders %}
                <li class="folder-item">
                    <span>📁 {{ folder }}</span>
                    <a href="{{ url_for('start_import', folder=folder) }}" class="btn btn-success">Importieren</a>
                </li>
                {% else %}
                <li class="folder-item">
                    <span style="color: #888;">Keine Hörbücher gefunden in {{ input_dir }}</span>
                </li>
                {% endfor %}
            </ul>
            
            <div class="library-section">
                <div class="section-header">
                    <h2>Bibliothek ({{ total_albums }} Alben):</h2>
                    <div class="controls">
                        <a href="{{ url_for('library_stats') }}" class="btn btn-primary btn-small">📊 Stats</a>
                    </div>
                </div>
                
                {% for artist, albums in library_items.items() %}
                <div class="artist-group">
                    <div class="artist-header" onclick="toggleArtist(this)">
                        <div class="artist-name">{{ artist }}</div>
                        <div class="artist-count">{{ albums|length }} Album(en)</div>
                    </div>
                    <div class="albums-container">
                        {% for album in albums %}
                        <div class="album-item">
                            <div class="album-info">
                                <div class="album-title">{{ album.album }}</div>
                                <div class="album-meta">
                                    {{ album.year or '?' }}
                                    {% if album.genre %} • {{ album.genre }}{% endif %}
                                </div>
                            </div>
                            <div class="album-actions">
                                <button onclick="showAlbumDetails('{{ album.id }}')" class="btn btn-info btn-small">ℹ️ Info</button>
                                <button onclick="editAlbum('{{ album.id }}')" class="btn btn-warning btn-small">✏️ Edit</button>
                                <a href="{{ url_for('delete_item', item_id=album.id) }}" 
                                   onclick="return confirm('Album wirklich löschen?')"
                                   class="btn btn-danger btn-small">✕</a>
                            </div>
                        </div>
                        {% endfor %}
                    </div>
                </div>
                {% else %}
                <div class="artist-group">
                    <div class="artist-header">
                        <span style="color: #888;">Bibliothek ist leer</span>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% else %}
        <div class="terminal-container">
            <div id="terminal">{{ terminal_output|safe }}</div>
        </div>
        
        <div class="shortcuts">
            <button class="shortcut-btn" onclick="sendShortcut('a')">A - Accept</button>
            <button class="shortcut-btn" onclick="sendShortcut('m')">M - More Candidates</button>
            <button class="shortcut-btn" onclick="sendShortcut('s')">S - Skip</button>
            <button class="shortcut-btn" onclick="sendShortcut('u')">U - Use as-is</button>
            <button class="shortcut-btn" onclick="sendShortcut('t')">T - As tracks</button>
            <button class="shortcut-btn" onclick="sendShortcut('g')">G - Group albums</button>
            <button class="shortcut-btn" onclick="sendShortcut('e')">E - Enter search</button>
            <button class="shortcut-btn" onclick="sendShortcut('i')">I - Enter ID</button>
            <button class="shortcut-btn" onclick="sendShortcut('b')">B - Abort</button>
            <button class="shortcut-btn" onclick="sendShortcut('d')">D - Edit</button>
            <button class="shortcut-btn" onclick="sendShortcut('c')">C - Edit candidates</button>
            <button class="shortcut-btn" onclick="sendShortcut('r')">R - Region switch</button>
        </div>
        
        <div class="input-area">
            <form method="post" action="{{ url_for('send_input') }}" class="input-form" id="input-form">
                <input type="text" id="input" name="text" placeholder="Eingabe..." autocomplete="off" autofocus>
                <button type="submit" class="btn btn-primary">Senden</button>
            </form>
        </div>
        {% endif %}
    </div>
    
    <!-- Album Details Modal -->
    <div id="albumModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 id="modalTitle">Album Details</h2>
                <span class="close" onclick="closeModal()">&times;</span>
            </div>
            <div class="modal-body" id="modalBody">
                <div class="detail-grid" id="detailsContainer"></div>
                <div class="track-list" id="trackList"></div>
            </div>
        </div>
    </div>
    
    <!-- Edit Album Modal -->
    <div id="editModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Album bearbeiten</h2>
                <span class="close" onclick="closeEditModal()">&times;</span>
            </div>
            <div class="modal-body">
                <form id="editForm" method="post" action="/edit_album">
                    <input type="hidden" id="editAlbumId" name="album_id">
                    <div class="detail-grid">
                        <label class="detail-label">Artist:</label>
                        <input type="text" name="albumartist" id="editArtist" style="background:#0c0c0c;color:#e0e0e0;border:1px solid #444;padding:8px;border-radius:4px;">
                        
                        <label class="detail-label">Album:</label>
                        <input type="text" name="album" id="editAlbumName" style="background:#0c0c0c;color:#e0e0e0;border:1px solid #444;padding:8px;border-radius:4px;">
                                                
                        <label class="detail-label">Jahr:</label>
                        <input type="text" name="year" id="editYear" style="background:#0c0c0c;color:#e0e0e0;border:1px solid #444;padding:8px;border-radius:4px;">
                        
                        <label class="detail-label">Genre:</label>
                        <input type="text" name="genre" id="editGenre" style="background:#0c0c0c;color:#e0e0e0;border:1px solid #444;padding:8px;border-radius:4px;">
                    </div>
                    <button type="submit" class="btn btn-success" style="width:100%;margin-top:16px;">Speichern</button>
                </form>
            </div>
        </div>
    </div>
    
    <script>
        {% if is_running %}
        function scrollTerminal() {
            const terminal = document.getElementById('terminal');
            terminal.scrollTop = terminal.scrollHeight;
        }
        function updateTerminal() {
            fetch('/terminal')
                .then(r => r.json())
                .then(data => {
                    if (data.open_path) {
                        window.location.href = '/edit?path=' + encodeURIComponent(data.open_path);
                        return;
                    }
                    const terminal = document.getElementById('terminal');
                    if (data.output !== terminal.textContent) {
                        terminal.innerHTML = data.output_html;
                        scrollTerminal();
                    }
                    if (!data.is_running) {
                        setTimeout(() => location.href = '/', 2000);
                    }
                });
        }
        function sendShortcut(key) {
            fetch('/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'text=' + encodeURIComponent(key)
            }).then(() => {
                setTimeout(updateTerminal, 100);
            });
        }
        setInterval(updateTerminal, 500);
        scrollTerminal();
        document.getElementById('input').focus();
        document.getElementById('input-form').onsubmit = function(e) {
            e.preventDefault();
            const input = document.getElementById('input');
            const text = input.value;
            fetch('/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'text=' + encodeURIComponent(text)
            }).then(() => {
                input.value = '';
                input.focus();
                setTimeout(updateTerminal, 100);
            });
            return false;
        };
        {% else %}
        function toggleArtist(header) {
            const container = header.nextElementSibling;
            container.classList.toggle('show');
        }
        
        function showAlbumDetails(albumId) {
            fetch('/album_details/' + albumId)
                .then(r => r.json())
                .then(data => {
                    if (data) {
                        document.getElementById('modalTitle').textContent = data.album || 'Album Details';
                        
                        let html = '<div class="detail-grid">';
                        html += '<div class="detail-label">Artist:</div><div class="detail-value">' + (data.albumartist || '-') + '</div>';
                        html += '<div class="detail-label">Album:</div><div class="detail-value">' + (data.album || '-') + '</div>';
                        html += '<div class="detail-label">Jahr:</div><div class="detail-value">' + (data.year || '-') + '</div>';
                        html += '<div class="detail-label">Genre:</div><div class="detail-value">' + (data.genre || '-') + '</div>';
                        html += '<div class="detail-label">Label:</div><div class="detail-value">' + (data.label || '-') + '</div>';
                        html += '<div class="detail-label">Katalognummer:</div><div class="detail-value">' + (data.catalognum || '-') + '</div>';
                        html += '<div class="detail-label">Land:</div><div class="detail-value">' + (data.country || '-') + '</div>';
                        html += '<div class="detail-label">Typ:</div><div class="detail-value">' + (data.albumtype || '-') + '</div>';
                        html += '<div class="detail-label">MusicBrainz ID:</div><div class="detail-value">' + (data.mb_albumid || '-') + '</div>';
                        html += '<div class="detail-label">Pfad:</div><div class="detail-value" style="font-size: 11px;">' + (data.path || '-') + '</div>';
                        html += '</div>';
                        
                        document.getElementById('detailsContainer').innerHTML = html;
                        
                        let trackHtml = '<h3>Tracks:</h3>';
                        if (data.tracks && data.tracks.length > 0) {
                            data.tracks.forEach(track => {
                                trackHtml += '<div class="track-item">';
                                trackHtml += '<div class="track-info">';
                                trackHtml += '<span class="track-number">' + (track.track || '?') + '.</span> ';
                                trackHtml += track.title || 'Unknown';
                                trackHtml += '</div>';
                                if (track.length) {
                                    trackHtml += '<div style="color: #aaa; font-size: 12px;">' + track.length + '</div>';
                                }
                                trackHtml += '</div>';
                            });
                        } else {
                            trackHtml += '<div style="color: #888;">Keine Tracks gefunden</div>';
                        }
                        
                        document.getElementById('trackList').innerHTML = trackHtml;
                        document.getElementById('albumModal').classList.add('show');
                    }
                });
        }
        
        function closeModal() {
            document.getElementById('albumModal').classList.remove('show');
        }
        
        function editAlbum(albumId) {
            fetch('/album_details/' + albumId)
                .then(r => r.json())
                .then(data => {
                    if (data) {
                        document.getElementById('editAlbumId').value = albumId;
                        document.getElementById('editArtist').value = data.albumartist || '';
                        document.getElementById('editAlbumName').value = data.album || '';
                        document.getElementById('editYear').value = data.year || '';
                        document.getElementById('editGenre').value = data.genre || '';
                        document.getElementById('editModal').classList.add('show');
                    }
                });
        }
        
        function closeEditModal() {
            document.getElementById('editModal').classList.remove('show');
        }
        
        window.onclick = function(event) {
            const modal = document.getElementById('albumModal');
            if (event.target === modal) {
                closeModal();
            }
        }
        {% endif %}
    </script>
</body>
</html>
"""

EDIT_TEMPLATE = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>YAML bearbeiten</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root{ --pad:16px; --gap:10px; --radius:8px; --font-mono:Menlo,Consolas,monospace; }
        body { background:#0c0c0c; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; margin:0; }
        .bar { display:flex; align-items:center; justify-content:space-between; gap: var(--gap); padding:12px var(--pad); background:#1f1f1f; border-bottom:1px solid #333; flex-wrap: wrap; }
        .path { font-size:12px; color:#aaa; word-break: break-all; }
        .btn { padding:10px 14px; border:0; border-radius:var(--radius); cursor:pointer; font-size:14px; }
        .btn-primary { background:#007acc; color:#fff; }
        .btn-secondary { background:#333; color:#ddd; margin-right:8px; }
        .wrap { padding:var(--pad); }
        textarea {
            width:100%;
            height:70vh;
            background:#0b0b0b;
            color:#e6e6e6;
            border:1px solid #333;
            border-radius:6px;
            padding:12px;
            font-family: var(--font-mono);
            font-size: clamp(12px, 2.5vw, 14px);
            line-height:1.45;
        }
        form { margin:0; }
        @media (max-width:700px){
            .btn { width:100%; margin-top:6px; }
            .btn-secondary { margin-right:0; }
        }
    </style>
</head>
<body>
    <div class="bar">
        <div>
            <strong>YAML bearbeiten</strong>
            <div class="path">{{ path }}</div>
        </div>
        <div>
            <a class="btn btn-secondary" href="{{ url_for('cancel_edit', path=path) }}">Abbrechen</a>
            <button form="saveForm" type="submit" class="btn btn-primary">Speichern & Fortfahren</button>
        </div>
    </div>
    <div class="wrap">
        <form id="saveForm" method="post" action="{{ url_for('save_edit') }}">
            <input type="hidden" name="path" value="{{ path }}">
            <textarea name="content">{{ content }}</textarea>
        </form>
    </div>
</body>
</html>
"""

# --- Helper Functions ---

def ansi_to_html(text):
    """Konvertiert ANSI codes zu HTML spans"""
    ansi_colors = {
        '30': 'black', '31': 'red', '32': 'green', '33': 'yellow',
        '34': 'blue', '35': 'magenta', '36': 'cyan', '37': 'white'
    }
    import html
    text = html.escape(text)
    for code, color in ansi_colors.items():
        text = text.replace(f'\x1b[{code}m', f'<span class="ansi-{color}">')
        text = text.replace(f'\x1b[1;{code}m', f'<span class="ansi-{color}">')
    text = text.replace('\x1b[0m', '</span>')
    text = text.replace('\x1b[m', '</span>')
    text = re.sub(r'\x1b\[[0-9;]*[mGKH]', '', text)
    return text

def find_import_folders():
    """Findet alle Ordner mit Audio-Dateien"""
    folders = []
    if os.path.isdir(INPUT_DIR):
        for root, dirs, files in os.walk(INPUT_DIR):
            if any(f.lower().endswith(('.m4b', '.m4a', '.mp3')) for f in files):
                rel_path = os.path.relpath(root, INPUT_DIR)
                if rel_path != '.':
                    folders.append(rel_path)
    return sorted(folders)

# --- Flask Routes ---

@app.route('/')
def index():
    library = get_library_items()
    total = sum(len(albums) for albums in library.values())
    
    return render_template_string(
        TEMPLATE,
        is_running=session.is_running(),
        current_folder=session.current_folder,
        folders=find_import_folders() if not session.is_running() else [],
        library_items=library if not session.is_running() else {},
        total_albums=total,
        terminal_output=ansi_to_html(session.get_output()),
        input_dir=INPUT_DIR
    )

@app.route('/terminal')
def terminal():
    """AJAX endpoint für Terminal-Updates"""
    output = session.get_output()
    return jsonify({
        'output': output,
        'output_html': ansi_to_html(output),
        'is_running': session.is_running(),
        'open_path': session.pending_editor_path
    })

@app.route('/album_details/<album_id>')
def album_details(album_id):
    """AJAX endpoint für Album-Details"""
    details = get_album_details(album_id)
    return jsonify(details)

@app.route('/start/<path:folder>')
def start_import(folder):
    """Startet einen neuen Import"""
    if session.start_import(folder):
        time.sleep(0.5)
    return redirect(url_for('index'))

@app.route('/send', methods=['POST'])
def send_input():
    """Sendet Input an den Prozess (AJAX)"""
    text = request.form.get('text', '')
    session.send_input(text)
    return '', 204

@app.route('/abort')
def abort():
    """Bricht den laufenden Import ab"""
    session.stop_import()
    return redirect(url_for('index'))

@app.route('/delete/<item_id>')
def delete_item(item_id):
    """Löscht ein Item aus der Bibliothek"""
    delete_library_item(item_id)
    return redirect(url_for('index'))

@app.route('/edit_album', methods=['POST'])
def edit_album():
    """Bearbeitet Album-Metadaten"""
    album_id = request.form.get('album_id', '')
    albumartist = request.form.get('albumartist', '')
    album = request.form.get('album', '')
    year = request.form.get('year', '')
    genre = request.form.get('genre', '')
    
    try:
        env = os.environ.copy()
        env["BEETSDIR"] = CONFIG_DIR
        
        # Baut modify command
        cmd = ["beet", "modify", "-y", f"id:{album_id}"]
        if albumartist:
            cmd.extend(["albumartist=" + albumartist])
        if album:
            cmd.extend(["album=" + album])
        if year:
            cmd.extend(["year=" + year])
        if genre:
            cmd.extend(["genre=" + genre])
        
        subprocess.run(cmd, env=env, timeout=10)
    except Exception as e:
        print(f"Error modifying album: {e}")
    
    return redirect(url_for('index'))

@app.route('/library_stats')
def library_stats():
    """Zeigt Library-Statistiken"""
    stats = get_library_stats()
    return f'<pre style="background:#0c0c0c;color:#00ff00;padding:20px;font-family:monospace">{stats}</pre><br><a href="/" style="color:#007acc">Zurück</a>'

@app.route('/edit')
def edit_yaml():
    """Zeigt die YAML zum Bearbeiten an"""
    path = request.args.get('path', '')
    if not path or not os.path.isfile(path):
        return redirect(url_for('index'))
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    return render_template_string(EDIT_TEMPLATE, path=path, content=content)

@app.route('/save_edit', methods=['POST'])
def save_edit():
    """Speichert YAML und setzt Fortsetzungssignal"""
    path = request.form.get('path', '')
    content = request.form.get('content', '')
    if path:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        open(path + '.done', 'w').close()
        session.pending_editor_path = None
    return redirect(url_for('index'))

@app.route('/cancel_edit')
def cancel_edit():
    """Bricht Bearbeitung ab und lässt beets fortfahren"""
    path = request.args.get('path', '')
    if path:
        open(path + '.done', 'w').close()
        session.pending_editor_path = None
    return redirect(url_for('index'))

if __name__ == '__main__':
    print("Starting Beets Web Terminal on port 5002...")
    app.run(host='0.0.0.0', port=5002, debug=False)

s