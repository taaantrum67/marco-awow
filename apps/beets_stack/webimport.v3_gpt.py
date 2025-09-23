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
        
        # Prozess starten
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

# --- HTML Templates ---
TEMPLATE = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Beets Webimport</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#1a1a1a; color:#e0e0e0; height:100vh; display:flex; flex-direction:column; }
        .header { background:#2a2a2a; padding:15px 20px; border-bottom:1px solid #444; display:flex; justify-content:space-between; align-items:center; }
        h1 { font-size:20px; color:#fff; }
        .controls { display:flex; gap:10px; }
        .btn { padding:8px 16px; border:0; border-radius:4px; cursor:pointer; font-size:14px; transition:opacity .2s; }
        .btn:hover { opacity:.8; }
        .btn-primary { background:#007acc; color:#fff; }
        .btn-danger { background:#d32f2f; color:#fff; }
        .btn-success { background:#4caf50; color:#fff; }
        .main-content { flex:1; display:flex; flex-direction:column; overflow:hidden; }
        .terminal-container { flex:1; background:#0c0c0c; overflow:hidden; display:flex; flex-direction:column; }
        #terminal { flex:1; padding:10px; overflow-y:auto; font-family:"Cascadia Code","SF Mono",Monaco,Consolas,monospace; font-size:13px; line-height:1.4; white-space:pre; word-wrap:break-word; color:#00ff00; }
        .ansi-black { color:#000; } .ansi-red { color:#cd3131; } .ansi-green { color:#0dbc79; } .ansi-yellow { color:#e5e510; } .ansi-blue { color:#2472c8; } .ansi-magenta { color:#bc3fbc; } .ansi-cyan { color:#11a8cd; } .ansi-white { color:#e5e5e5; }
        .input-area { background:#1a1a1a; border-top:1px solid #444; padding:15px; }
        .input-form { display:flex; gap:10px; }
        #input { flex:1; background:#0c0c0c; color:#00ff00; border:1px solid #444; padding:10px; font-family:"Cascadia Code","SF Mono",Monaco,Consolas,monospace; font-size:14px; border-radius:4px; }
        #input:focus { outline:none; border-color:#007acc; }
        .folder-selection { padding:40px; max-width:800px; margin:0 auto; }
        .folder-list { list-style:none; margin-top:20px; }
        .folder-item { background:#2a2a2a; border:1px solid #444; border-radius:4px; padding:15px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; }
        .folder-item:hover { background:#333; }
        .status { padding:5px 10px; background:#333; border-radius:4px; font-size:12px; color:#aaa; }
        #terminal::-webkit-scrollbar { width:10px; } #terminal::-webkit-scrollbar-track { background:#1a1a1a; } #terminal::-webkit-scrollbar-thumb { background:#444; border-radius:5px; } #terminal::-webkit-scrollbar-thumb:hover { background:#555; }
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
            <h2>Wähle ein Hörbuch zum Import:</h2>
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
        </div>
        {% else %}
        <div class="terminal-container">
            <div id="terminal">{{ terminal_output|safe }}</div>
        </div>
        
        <div class="input-area">
            <form method="post" action="{{ url_for('send_input') }}" class="input-form" id="input-form">
                <input type="text" id="input" name="text" placeholder="Eingabe..." autocomplete="off" autofocus>
                <button type="submit" class="btn btn-primary">Senden</button>
            </form>
        </div>
        {% endif %}
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
    <style>
        body { background:#0c0c0c; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; margin:0; }
        .bar { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; background:#1f1f1f; border-bottom:1px solid #333; }
        .path { font-size:12px; color:#aaa; }
        .btn { padding:8px 14px; border:0; border-radius:4px; cursor:pointer; font-size:14px; }
        .btn-primary { background:#007acc; color:#fff; }
        .btn-secondary { background:#333; color:#ddd; margin-right:8px; }
        .wrap { padding:16px; }
        textarea { width:100%; height:70vh; background:#0b0b0b; color:#e6e6e6; border:1px solid #333; border-radius:6px; padding:12px; font-family:Menlo,Consolas,monospace; font-size:13px; line-height:1.45; }
        form { margin:0; }
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
    return render_template_string(
        TEMPLATE,
        is_running=session.is_running(),
        current_folder=session.current_folder,
        folders=find_import_folders() if not session.is_running() else [],
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
        # kein Schreibvorgang, nur Fortsetzen
        open(path + '.done', 'w').close()
        session.pending_editor_path = None
    return redirect(url_for('index'))

if __name__ == '__main__':
    print("Starting Beets Web Terminal on port 5002...")
    app.run(host='0.0.0.0', port=5002, debug=False)
