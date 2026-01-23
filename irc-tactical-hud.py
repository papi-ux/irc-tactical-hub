import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading, os, time, subprocess, webbrowser, ctypes, json, sqlite3, logging, re, glob
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

# --- CONSTANTS & CONFIGURATION ---
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "bridge_dir": r"C:\speedtest",
    "irc_log": "", 
    "ntfy_topic": "interview_alerts",
    "bot_name": "Gatekeeper",
    "client_title": "mIRC",
    "user_nick": "",
    # Default Priorities
    "prio_queue": "low",       # Queue moving (other people)
    "prio_mention": "urgent",  # You are mentioned
    "prio_top5": "high",       # You are in top 5
    "prio_netsplit": "high"    # Server split
}

# Pre-compile Regex Patterns
RE_POSITION = re.compile(r"position\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
RE_CURRENTLY = re.compile(r"currently\s+#(\d+)", re.IGNORECASE)
RE_NETSPLIT = re.compile(r"(\*\.net \*\.split|Quit: \*\.net \*\.split)", re.IGNORECASE)
RE_KICK = re.compile(r"kicked by", re.IGNORECASE)
RE_COLOR_STRIP = re.compile(r'[\x02\x0F\x16\x1F]|\x03\d{0,2}(?:,\d{0,2})?')

# Check for requests library and setup Retry Logic
HAS_REQUESTS = False
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_REQUESTS = True
except ImportError:
    pass

# --- WIN32 API HELPER ---
user32 = ctypes.windll.user32

def focus_window(partial_title):
    """Finds a window by partial title and brings it to foreground."""
    found_hwnd = None
    
    def callback(hwnd, extra):
        nonlocal found_hwnd
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value
            if partial_title.lower() in title.lower():
                found_hwnd = hwnd
                return 0 
        return 1

    CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(CMPFUNC(callback), 0)
    
    if found_hwnd:
        if user32.IsIconic(found_hwnd):
            user32.ShowWindow(found_hwnd, 9) 
        user32.SetForegroundWindow(found_hwnd)
        return True
    return False

# --- DATABASE CLASS ---
class InterviewDatabase:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.init_database()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL') # Write-Ahead Logging for concurrency
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_database(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS interviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    outcome_message TEXT
                )
            ''')
            # Create indices for faster queries
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ts ON interviews(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user ON interviews(username)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_type ON interviews(event_type)')

    def clear_all_data(self):
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM interviews")
            return True
        except Exception as e:
            print(f"DB Error: {e}")
            return False

    def record_event(self, username, event_type, message=None, timestamp=None):
        ts = timestamp if timestamp else datetime.now().isoformat()
        try:
            dt_ts = datetime.fromisoformat(ts)
        except:
            dt_ts = datetime.now()
        
        window_start = (dt_ts - timedelta(hours=12)).isoformat()
        window_end = (dt_ts + timedelta(hours=12)).isoformat()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Check for duplicates efficiently using indices
            cursor.execute('''
                SELECT id FROM interviews 
                WHERE username = ? AND event_type = ? AND timestamp BETWEEN ? AND ?
            ''', (username, event_type, window_start, window_end))
            
            if cursor.fetchone():
                return False 

            conn.execute('INSERT INTO interviews (username, timestamp, event_type, outcome_message) VALUES (?, ?, ?, ?)',
                         (username, ts, event_type, message))
            return True

    def get_stats(self, hours=24):
        start_date = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) as total FROM interviews WHERE event_type='started' AND timestamp >= ?", (start_date,))
            total = cursor.fetchone()['total']

            cursor.execute("""
                SELECT strftime('%H', timestamp) as hour, COUNT(*) as count 
                FROM interviews 
                WHERE event_type='started' AND timestamp >= ? 
                GROUP BY hour ORDER BY count DESC LIMIT 1
            """, (start_date,))
            busy = cursor.fetchone()
            busiest = f"{busy['hour']}:00" if busy else "N/A"

            cursor.execute("SELECT username, timestamp FROM interviews WHERE event_type='started' ORDER BY timestamp DESC LIMIT 10")
            recent = [dict(row) for row in cursor.fetchall()]
            
            cursor.execute("SELECT event_type, COUNT(*) as count FROM interviews WHERE event_type IN ('passed', 'failed', 'missed') AND timestamp >= ? GROUP BY event_type", (start_date,))
            outcomes = {row['event_type']: row['count'] for row in cursor.fetchall()}

            return {'total': total, 'busiest': busiest, 'recent': recent, 'outcomes': outcomes}

    def get_velocity(self, hours=3):
        start_date = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as total FROM interviews WHERE event_type='started' AND timestamp >= ?", (start_date,))
            total = cursor.fetchone()['total']
            return round(total / hours, 1)

# --- SETTINGS WINDOW ---
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, config, callback):
        super().__init__(parent)
        self.title("Settings")
        self.geometry("750x700")
        self.configure(bg="#1a1a1a")
        self.config = config
        self.callback = callback
        self.create_widgets()

    def create_widgets(self):
        pad = {'padx': 15, 'pady': 5}
        PRIORITIES = ["min", "low", "default", "high", "urgent"]
        
        tk.Label(self, text="SYSTEM CONFIGURATION", bg="#1a1a1a", fg="#00ff00", font=("Consolas", 14, "bold")).pack(pady=10)
        
        fields = [
            ("Bridge Directory:", "bridge_dir"),
            ("IRC Client Log File:", "irc_log"),
            ("Bot Name:", "bot_name"),
            ("Window Title:", "client_title"),
            ("Your Nickname:", "user_nick"),
            ("Ntfy Topic:", "ntfy_topic")
        ]

        self.entries = {}
        for label_text, config_key in fields:
            tk.Label(self, text=label_text, bg="#1a1a1a", fg="#00ffff", font=("Consolas", 10, "bold")).pack(anchor="w", padx=15, pady=(2, 0))
            frame = tk.Frame(self, bg="#1a1a1a")
            frame.pack(fill=tk.X, **pad)
            entry = tk.Entry(frame, width=60, bg="#333", fg="white", font=("Consolas", 9))
            entry.insert(0, self.config.get(config_key, ""))
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.entries[config_key] = entry
            if config_key == "irc_log":
                tk.Button(frame, text="Browse", command=self.browse_log, bg="#444", fg="white", font=("Consolas", 9)).pack(side=tk.RIGHT, padx=5)

        ttk.Separator(self, orient='horizontal').pack(fill='x', pady=15)
        
        tk.Label(self, text="NOTIFICATION PRIORITIES", bg="#1a1a1a", fg="#ffaa00", font=("Consolas", 14, "bold")).pack(pady=5)
        prio_frame = tk.Frame(self, bg="#1a1a1a")
        prio_frame.pack(fill=tk.X, padx=15, pady=5)
        
        prio_fields = [
            ("Queue Moving (Other users):", "prio_queue"),
            ("Mention Alert (Your Name):", "prio_mention"),
            ("Top 5 Alert:", "prio_top5"),
            ("Netsplit Alert:", "prio_netsplit")
        ]
        
        self.combos = {}
        for idx, (label, key) in enumerate(prio_fields):
            row_frame = tk.Frame(prio_frame, bg="#1a1a1a")
            row_frame.pack(fill=tk.X, pady=2)
            tk.Label(row_frame, text=label, bg="#1a1a1a", fg="#cccccc", font=("Consolas", 10), width=30, anchor="w").pack(side=tk.LEFT)
            cb = ttk.Combobox(row_frame, values=PRIORITIES, state="readonly", width=15)
            cb.set(self.config.get(key, "default"))
            cb.pack(side=tk.LEFT)
            self.combos[key] = cb

        tk.Button(self, text="💾 SAVE SETTINGS", command=self.save, bg="#00ff00", fg="black", font=("Consolas", 11, "bold"), pady=5).pack(pady=20)

    def browse_log(self):
        f = filedialog.askopenfilename(filetypes=[("Log Files", "*.log"), ("All Files", "*.*")])
        if f:
            self.entries["irc_log"].delete(0, tk.END)
            self.entries["irc_log"].insert(0, f)

    def save(self):
        new_conf = {key: entry.get().strip() for key, entry in self.entries.items()}
        for key, cb in self.combos.items():
            new_conf[key] = cb.get()
        self.callback(new_conf)
        self.destroy()

# --- MAIN HUD CLASS ---
class UniversalHUD(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("mirc-tactical-hud v5.17 - OPTIMIZED")
        self.geometry("1100x950") 
        self.configure(bg="#0a0a0a")
        self.attributes('-topmost', True)
        
        self.load_config()
        self.init_paths()
        
        self.db = InterviewDatabase(self.db_file)
        self.last_pos = "?"
        self.current_rank = 999
        self.netsplit_count = 0
        self.top5_alert_sent = False
        
        self.last_netsplit_alert = 0 
        self.kick_counter = []
        self.parser_running = False
        self.last_clipboard = "" 
        
        self.setup_ui()
        self.start_monitoring()
        self.start_log_parser()

    def setup_ui(self):
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure("TNotebook", background="#0a0a0a", borderwidth=0)
        self.style.configure("TNotebook.Tab", background="#333", foreground="white", padding=[10, 5])
        self.style.map("TNotebook.Tab", background=[("selected", "#00ff00")], foreground=[("selected", "black")])

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.tab_hud = tk.Frame(self.notebook, bg="#0a0a0a")
        self.tab_intel = tk.Frame(self.notebook, bg="#0a0a0a")
        
        self.notebook.add(self.tab_hud, text=" 🛡️ TACTICAL ")
        self.notebook.add(self.tab_intel, text=" 📊 INTEL / STATS ")

        self.create_hud_widgets()
        self.create_intel_widgets()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f: self.config = json.load(f)
            except: self.config = DEFAULT_CONFIG
        else: self.config = DEFAULT_CONFIG

    def save_config(self, new_config):
        self.config = new_config
        with open(CONFIG_FILE, "w") as f: json.dump(self.config, f)
        self.init_paths()
        self.log("SYSTEM: Configuration updated.")
        self.reload_parser() 

    def init_paths(self):
        self.bridge_dir = self.config.get("bridge_dir", DEFAULT_CONFIG["bridge_dir"])
        self.irc_log = self.config.get("irc_log", DEFAULT_CONFIG["irc_log"])
        self.ntfy_topic = self.config.get("ntfy_topic", DEFAULT_CONFIG["ntfy_topic"])
        self.bot_name = self.config.get("bot_name", DEFAULT_CONFIG["bot_name"])
        self.client_title = self.config.get("client_title", DEFAULT_CONFIG["client_title"])
        self.user_nick = self.config.get("user_nick", DEFAULT_CONFIG["user_nick"])
        
        try:
            if not os.path.exists(self.bridge_dir): os.makedirs(self.bridge_dir, exist_ok=True)
        except: pass

        self.st_json = os.path.join(self.bridge_dir, "st_result.json")
        self.link_file = os.path.join(self.bridge_dir, "queue_link.txt")
        self.db_file = os.path.join(self.bridge_dir, "interview_stats.db")

    def create_hud_widgets(self):
        top_bar = tk.Frame(self.tab_hud, bg="#111", height=150, highlightbackground="#333", highlightthickness=1)
        top_bar.pack(fill=tk.X, padx=10, pady=10)

        def add_stat_box(parent, title, var, color="white", font_size=20):
            container = tk.Frame(parent, bg="#111")
            container.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
            tk.Label(container, text=title, bg="#111", fg="#888", font=("Consolas", 10)).pack()
            lbl = tk.Label(container, textvariable=var, bg="#111", fg=color, font=("Consolas", font_size, "bold"))
            lbl.pack()
            return lbl

        self.pos_var = tk.StringVar(value="# ?")
        self.pos_lbl = add_stat_box(top_bar, "QUEUE RANK", self.pos_var, "#00ff00", 60)

        eta_container = tk.Frame(top_bar, bg="#111")
        eta_container.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
        tk.Label(eta_container, text="QUEUE VELOCITY", bg="#111", fg="#888", font=("Consolas", 10)).pack()
        self.velocity_var = tk.StringVar(value="--/hr")
        tk.Label(eta_container, textvariable=self.velocity_var, bg="#111", fg="#00ffff", font=("Consolas", 20, "bold")).pack()
        tk.Label(eta_container, text="EST. WAIT", bg="#111", fg="#888", font=("Consolas", 10)).pack(pady=(5,0))
        self.eta_var = tk.StringVar(value="Calculating...")
        tk.Label(eta_container, textvariable=self.eta_var, bg="#111", fg="#ffaa00", font=("Consolas", 20, "bold")).pack()

        self.ns_var = tk.StringVar(value="0")
        add_stat_box(top_bar, "NETSPLITS", self.ns_var, "orange", 40)

        # Link Frame
        link_frame = tk.Frame(self.tab_hud, bg="#0a0a0a", pady=5)
        link_frame.pack(fill=tk.X, padx=10)
        tk.Label(link_frame, text="MANUAL SPEEDTEST LINK:", bg="#0a0a0a", fg="#00ffff", font=("Consolas", 10)).pack(side=tk.LEFT)
        self.link_entry = tk.Entry(link_frame, bg="#222", fg="#fff", font=("Consolas", 10), width=40)
        self.link_entry.pack(side=tk.LEFT, padx=5)
        tk.Button(link_frame, text="💾 SAVE", command=self.save_manual_link, bg="#00ffff", fg="black", font=("Consolas", 9, "bold")).pack(side=tk.LEFT, padx=5)
        tk.Button(link_frame, text="🌐 WEB", command=lambda: webbrowser.open("https://www.speedtest.net/"), bg="#333", fg="#fff", font=("Consolas", 9)).pack(side=tk.LEFT, padx=5)

        # --- QUICK ACTIONS ---
        action_lbl = tk.LabelFrame(self.tab_hud, text=" QUICK ACTIONS ", bg="#0a0a0a", fg="#00ff00", font=("Consolas", 10, "bold"))
        action_lbl.pack(fill=tk.X, padx=10, pady=5)
        
        q_btns = [
            ("🎯 FOCUS CLIENT", self.focus_client, "#cc0000"),
            ("🚀 AUTO-TEST", self.run_and_auto_copy, "#0000AA"),
            ("📋 COPY !QUEUE", self.copy_queue_cmd, "#00ff00"), 
            ("⚡ CLI ONLY", self.run_st, "#555555"),
            ("🛡️ SYS CHECK", self.check_system_health, "#008888")
        ]
        
        for txt, cmd, fg in q_btns:
            tk.Button(action_lbl, text=txt, command=cmd, bg="#333", fg=fg if fg else "white", font=("Consolas", 9, "bold")).pack(side=tk.LEFT, padx=5, pady=5)

        # --- MIRC SCRIPT ALIASES ---
        alias_lbl = tk.LabelFrame(self.tab_hud, text=" mIRC SCRIPT ALIASES ", bg="#0a0a0a", fg="#00ffff", font=("Consolas", 10, "bold"))
        alias_lbl.pack(fill=tk.X, padx=10, pady=5)
        
        m_btns = [
            ("🔄 CHECK POS (/request_pos)", "/request_pos", "#008800"),
            ("🔁 RE-QUEUE (/rq)", "/rq", "#008888"),
            ("⚙️ SETUP (/hud_setup)", "/hud_setup", "#555"),
            ("⚡ INIT (/hud_init)", "/hud_init", "#555"),
            ("🐞 DEBUG (/hud_debug)", "/hud_debug", "#777")
        ]
        
        for txt, alias_cmd, bg in m_btns:
            tk.Button(alias_lbl, text=txt, 
                      command=lambda c=alias_cmd: self.copy_command(c), 
                      bg=bg, fg="white", font=("Consolas", 9, "bold")).pack(side=tk.LEFT, padx=5, pady=5)

        # --- SYSTEM ---
        sys_frame = tk.Frame(self.tab_hud, bg="#0a0a0a")
        sys_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Button(sys_frame, text="🔔 TEST NTFY", command=self.test_ntfy, bg="#333", fg="#FFA500", font=("Consolas", 9)).pack(side=tk.LEFT)
        tk.Button(sys_frame, text="⚙️ SETTINGS", command=self.open_settings, bg="#555", fg="#fff", font=("Consolas", 9)).pack(side=tk.RIGHT)
        tk.Button(sys_frame, text="♻️ RELOAD", command=self.reload_parser, bg="#444", fg="#00ffff", font=("Consolas", 9, "bold")).pack(side=tk.RIGHT, padx=5)

        # Logs
        log_frame = tk.LabelFrame(self.tab_hud, text=" SYSTEM LOGS ", bg="#0a0a0a", fg="#00ff00")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.log_txt = tk.Text(log_frame, bg="#050505", fg="#00ff00", font=("Consolas", 9), state='disabled')
        self.log_txt.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="SYSTEM READY - WAITING FOR DATA")
        self.status_bar = tk.Label(self.tab_hud, textvariable=self.status_var, bg="#333", fg="white", font=("Consolas", 10, "bold"), pady=5)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def check_system_health(self):
        self.log("--- SYSTEM HEALTH CHECK ---")
        issues = []
        
        # 1. Bridge Dir
        if os.path.exists(self.bridge_dir):
            self.log(f"[PASS] Bridge Dir: {self.bridge_dir}")
        else:
            self.log(f"[FAIL] Bridge Dir missing: {self.bridge_dir}")
            issues.append("Bridge Directory not found")

        # 2. Queue Link (Critical for Netsplit)
        if os.path.exists(self.link_file):
            with open(self.link_file, 'r') as f:
                link = f.read().strip()
            if link.startswith("http"):
                mtime = os.path.getmtime(self.link_file)
                age_mins = int((time.time() - mtime) / 60)
                self.log(f"[PASS] Auto-Queue Link: READY ({age_mins}m old)")
                self.log(f"       -> {link}")
            else:
                self.log("[FAIL] Auto-Queue Link: Invalid or Empty")
                issues.append("Queue Link is invalid")
        else:
            self.log("[FAIL] Auto-Queue Link: NOT SAVED")
            issues.append("No Speedtest Link saved (Run Auto-Test!)")

        # 3. Log File
        if os.path.exists(self.irc_log) and os.path.isfile(self.irc_log):
             self.log(f"[PASS] Log Parser: Connected to {os.path.basename(self.irc_log)}")
        else:
             self.log(f"[WARN] Log Parser: File not found ({self.irc_log})")
             issues.append("Log file path invalid")

        # 4. Bot Name
        if self.bot_name:
             self.log(f"[PASS] Target Bot: {self.bot_name}")
        else:
             self.log("[FAIL] Target Bot: Not configured")
             issues.append("Bot Name missing in Settings")

        self.log("-" * 30)
        
        if not issues:
            self.log("✅ SYSTEM STATUS: GREEN (READY)")
            self.status_var.set("SYSTEM HEALTH: 100% READY")
            self.status_bar.config(bg="#00ff00", fg="black")
            messagebox.showinfo("System Check", "All Systems Go!\n\nIf a netsplit occurs, mIRC will find the saved link and auto-queue.")
        else:
            self.log(f"❌ SYSTEM STATUS: RED ({len(issues)} Issues)")
            self.status_var.set("SYSTEM HEALTH: ACTION REQUIRED")
            self.status_bar.config(bg="red", fg="white")
            messagebox.showwarning("System Check", "Issues Detected:\n- " + "\n- ".join(issues))

    def copy_command(self, cmd_text):
        """Helper to copy any text to clipboard and focus client"""
        self.clipboard_clear()
        self.clipboard_append(cmd_text)
        self.focus_client()
        self.log(f"COPIED: {cmd_text}")

    def create_intel_widgets(self):
        frame = tk.Frame(self.tab_intel, bg="#0a0a0a", padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)
        header = tk.Frame(frame, bg="#0a0a0a")
        header.pack(fill=tk.X, pady=(0, 20))
        tk.Label(header, text="INTERVIEW ANALYTICS (24H)", bg="#0a0a0a", fg="#00ff00", font=("Consolas", 16, "bold")).pack(side=tk.LEFT)
        btn_box = tk.Frame(header, bg="#0a0a0a")
        btn_box.pack(side=tk.RIGHT)
        tk.Button(btn_box, text="🗑️ RESET DATA", command=self.reset_db, bg="#550000", fg="#ff9999", font=("Consolas", 10, "bold")).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_box, text="🔄 IMPORT HISTORY", command=self.import_history, bg="#333", fg="#00ffff", font=("Consolas", 10, "bold")).pack(side=tk.RIGHT, padx=5)
        stats_grid = tk.Frame(frame, bg="#0a0a0a")
        stats_grid.pack(fill=tk.X)
        def add_intel_box(title, var, color):
            container = tk.Frame(stats_grid, bg="#111", padx=10, pady=10)
            container.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
            tk.Label(container, text=title, bg="#111", fg="#888").pack()
            tk.Label(container, textvariable=var, bg="#111", fg=color, font=("Consolas", 24, "bold")).pack()
        self.stat_total = tk.StringVar(value="0")
        add_intel_box("TOTAL INTERVIEWS", self.stat_total, "#00ffff")
        self.stat_busy = tk.StringVar(value="--:--")
        add_intel_box("BUSIEST HOUR", self.stat_busy, "#ff00ff")
        self.stat_outcomes = tk.StringVar(value="P:0 F:0 M:0")
        add_intel_box("OUTCOMES", self.stat_outcomes, "#ffff00")
        tk.Label(frame, text="RECENTLY STARTED", bg="#0a0a0a", fg="#00ff00", font=("Consolas", 12, "bold")).pack(pady=(30, 10), anchor="w")
        self.recent_list = tk.Listbox(frame, bg="#111", fg="#00ff00", font=("Consolas", 10), height=15, relief=tk.FLAT)
        self.recent_list.pack(fill=tk.BOTH, expand=True)

    def open_settings(self):
        SettingsDialog(self, self.config, self.save_config)

    def reset_db(self):
        if messagebox.askyesno("RESET DATABASE", "Are you sure?"):
            if self.db.clear_all_data():
                self.update_intel()
                self.log("HISTORY: Database cleared.")

    def import_history(self):
        if not os.path.exists(self.irc_log) or os.path.isdir(self.irc_log):
            messagebox.showerror("Error", f"Invalid log path:\n{self.irc_log}\nCheck settings.")
            return
        if not messagebox.askyesno("Import", "Scan log file?"): return
        self.log("HISTORY: Starting import...")
        threading.Thread(target=self._process_history, daemon=True).start()

    def _process_history(self):
        try:
            count = 0
            if os.path.isdir(self.irc_log): raise FileNotFoundError(f"Path is dir: {self.irc_log}")
            with open(self.irc_log, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = RE_COLOR_STRIP.sub('', line)
                    if (self.bot_name in line) and ("Now interviewing:" in line or "Currently interviewing:" in line):
                        username = self._extract_username_start(line)
                        if username and self.db.record_event(username, "started"): count += 1
                    if f"kicked by {self.bot_name}" in line:
                        username, event_type, reason = self._extract_outcome(line)
                        if username and event_type and self.db.record_event(username, event_type, reason): count += 1
            self.after(0, lambda: self.log(f"HISTORY: Imported {count} events."))
            self.after(0, self.update_intel)
        except Exception as e:
            err = str(e)
            self.after(0, lambda: self.log(f"IMPORT ERROR: {err}"))

    def _extract_username_start(self, line):
        try:
            if "Currently interviewing:" in line:
                parts = line.split("Currently interviewing:", 1)[1].strip()
                return parts.split(":::")[0].strip() if ":::" in parts else parts.split()[0]
            else: return line.split("Now interviewing:", 1)[1].strip().split()[0]
        except: return None

    def _extract_outcome(self, line):
        try:
            parts = line.split(f"was kicked by {self.bot_name}")
            if len(parts) > 1:
                username = parts[0].strip().split()[-1]
                reason = parts[1].strip()
                event_type = "passed" if "Congratulations!" in reason else "failed" if "not passed" in reason else "missed" if "missed your interview" in reason else None
                return username, event_type, reason
        except: pass
        return None, None, None

    def update_intel(self):
        try:
            stats = self.db.get_stats(hours=24)
            self.stat_total.set(str(stats['total']))
            self.stat_busy.set(str(stats['busiest']))
            o = stats.get('outcomes', {})
            self.stat_outcomes.set(f"P:{o.get('passed',0)} F:{o.get('failed',0)} M:{o.get('missed',0)}")
            self.recent_list.delete(0, tk.END)
            for item in stats['recent']:
                ts = datetime.fromisoformat(item['timestamp']).strftime('%H:%M')
                self.recent_list.insert(tk.END, f"[{ts}] {item['username']}")
        except Exception as e: print(f"Intel Error: {e}")

    def log(self, msg):
        self.log_txt.config(state='normal')
        self.log_txt.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.log_txt.see(tk.END)
        self.log_txt.config(state='disabled')

    def send_ntfy(self, title, message, priority="default"):
        if HAS_REQUESTS and self.ntfy_topic:
            threading.Thread(target=self._ntfy_thread, args=(title, message, priority), daemon=True).start()

    def _ntfy_thread(self, title, message, priority):
        try:
            # FIX: Automatic Retry logic for SSL/Network issues
            session = requests.Session()
            retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
            session.mount('https://', HTTPAdapter(max_retries=retries))
            
            session.post(
                f"https://ntfy.sh/{self.ntfy_topic}", 
                data=message.encode('utf-8'), 
                headers={"Title": title, "Priority": priority},
                timeout=10
            )
            self.log(f"Ntfy Sent: {title}")
        except Exception as e:
            self.log(f"Ntfy Error: {e}")

    def focus_client(self):
        if not focus_window(self.client_title): self.log(f"ERROR: Window '{self.client_title}' not found.")

    def save_manual_link(self):
        url = self.link_entry.get().strip()
        if url:
            try:
                with open(self.link_file, "w") as f: f.write(url)
                self.log(f"Saved: {url}")
                self.status_var.set("READY (MANUAL LINK)")
                self.status_bar.config(bg="#00ff00", fg="black")
            except Exception as e: self.log(f"Save Error: {e}")

    def run_st(self):
        self.log("HUD: Direct Speedtest Initiated...")
        self._exec_speedtest()

    def run_and_auto_copy(self):
        self.log("HUD: Auto-Test Started...")
        self.status_var.set("TEST RUNNING...")
        self.status_bar.config(bg="yellow", fg="black")
        self._exec_speedtest(auto_copy=True)

    def _exec_speedtest(self, auto_copy=False):
        exe = os.path.join(self.bridge_dir, "speedtest.exe")
        if not os.path.exists(exe):
            self.log("ERROR: speedtest.exe not found.")
            return
        
        def _bg():
            try:
                info = subprocess.STARTUPINFO()
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = 0
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                with open(self.st_json, "w") as out:
                    subprocess.run([exe, "--accept-license", "--accept-gdpr", "--format=json"], cwd=self.bridge_dir, stdout=out, stderr=subprocess.PIPE, creationflags=flags, startupinfo=info)
                self.log("ENGINE: Speedtest finished.")
                if auto_copy: 
                    self.after(0, self.copy_queue_cmd)
                    self.after(0, lambda: self.status_var.set("TEST COMPLETE - COPIED"))
                    self.after(0, lambda: self.status_bar.config(bg="#00ff00"))
            except Exception as e: self.log(f"Engine Error: {e}")
        threading.Thread(target=_bg, daemon=True).start()

    def test_ntfy(self):
        self.send_ntfy("HUD Test", "Notification system active.", "high")

    def req_pos(self):
        self.clipboard_clear()
        cmd = f"/msg {self.bot_name} !position"
        self.clipboard_append(cmd)
        self.focus_client()
        self.log(f"COPIED: {cmd}")

    def copy_queue_cmd(self):
        try:
            # Check manual link first
            if os.path.exists(self.link_file):
                with open(self.link_file, "r") as f:
                    url = f.read().strip()
                    if url:
                        self._copy(url)
                        return
            # Fallback to JSON
            if os.path.exists(self.st_json):
                with open(self.st_json, "r") as f:
                    data = json.load(f)
                    sid = data.get("result", {}).get("id") or data.get("id")
                    if sid:
                        url = f"https://www.speedtest.net/result/c/{sid}" if "-" in str(sid) else f"https://www.speedtest.net/result/{sid}.png"
                        self._copy(url)
        except Exception: pass

    def _copy(self, url):
        cmd = f"!queue {url}"
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self.log(f"COPIED: {cmd}")
        self.focus_client()
        # Thread-safe GUI update
        self.after(0, lambda u=url: self._update_link_ui(u))

    def _update_link_ui(self, url):
        self.link_entry.delete(0, tk.END)
        self.link_entry.insert(0, url)
        self.save_manual_link()

    def flash_alert(self):
        bg = self.cget("bg")
        self.configure(bg="#ff0000" if bg == "#0a0a0a" else "#0a0a0a")
        self.bell()
        count = getattr(self, "flash_count", 0)
        if count < 6:
            self.flash_count = count + 1
            self.after(200, self.flash_alert)
        else:
            self.configure(bg="#0a0a0a")
            self.flash_count = 0

    def reload_parser(self):
        self.log("SYSTEM: Reloading Log Parser...")
        self.status_var.set("RELOADING ENGINE...")
        self.status_bar.config(bg="purple", fg="white")
        self.parser_running = False
        self.log_txt.config(state='normal')
        self.log_txt.delete(1.0, tk.END)
        self.log_txt.config(state='disabled')
        self.after(1000, self.start_log_parser)

    def start_log_parser(self):
        self.parser_running = True
        
        def scan_log(filepath):
            try:
                if not os.path.exists(filepath): return
                self.after(0, lambda: self.log(f"PARSER: Monitoring {os.path.basename(filepath)}"))
                
                f_size = os.path.getsize(filepath)
                read_size = min(51200, f_size)
                
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    self.after(0, lambda: self.status_var.set("SCANNING RECENT LOGS..."))
                    self.after(0, lambda: self.status_bar.config(bg="purple", fg="white"))
                    
                    if f_size > read_size: f.seek(f_size - read_size)
                    
                    for line in f.readlines():
                        self._process_log_line(line, live=False)
                    
                    self.after(0, lambda: self.status_var.set("MONITORING LIVE LOGS"))
                    self.after(0, lambda: self.status_bar.config(bg="#00ff00", fg="black"))
                    
                    while self.parser_running:
                        line = f.readline()
                        if not line:
                            time.sleep(1)
                            continue
                        self._process_log_line(line, live=True)
            except Exception as e:
                self.after(0, lambda: self.log(f"Parser Error ({os.path.basename(filepath)}): {e}"))

        if self.irc_log and os.path.exists(self.irc_log):
            threading.Thread(target=scan_log, args=(self.irc_log,), daemon=True).start()

        if self.irc_log and self.bot_name:
            log_dir = os.path.dirname(self.irc_log)
            potential_files = glob.glob(os.path.join(log_dir, f"{self.bot_name}*.log"))
            for p_log in potential_files:
                if os.path.abspath(p_log) != os.path.abspath(self.irc_log):
                    self.after(0, lambda p=p_log: self.log(f"AUTO-DETECT: Found Bot Log: {os.path.basename(p)}"))
                    threading.Thread(target=scan_log, args=(p_log,), daemon=True).start()
                    break

    def _process_log_line(self, line, live=True):
        line = RE_COLOR_STRIP.sub('', line)
        line_lower = line.lower()
        bot_lower = self.bot_name.lower()
        
        # Get Priorities from Config
        prio_queue = self.config.get("prio_queue", "low")
        prio_netsplit = self.config.get("prio_netsplit", "high")
        prio_mention = self.config.get("prio_mention", "urgent")

        if bot_lower in line_lower:
            m1 = RE_POSITION.search(line)
            if m1:
                self.current_rank = int(m1.group(1))
                self.pos_var.set(f"{m1.group(1)} / {m1.group(2)}")
                if live: self.log(f"RANK UPDATE: {m1.group(1)}")
            
            m2 = RE_CURRENTLY.search(line)
            if m2:
                self.current_rank = int(m2.group(1))
                self.pos_var.set(f"# {m2.group(1)}")
                if live: self.log(f"RANK UPDATE: {m2.group(1)}")

            if "now interviewing:" in line_lower or "currently interviewing:" in line_lower:
                if live:
                    user = self._extract_username_start(line)
                    if user:
                        self.db.record_event(user, "started")
                        self.log(f"STATS: Interview started ({user})")
                        self.send_ntfy("Queue Moving", f"{user} is being interviewed", prio_queue)
                        self.after(0, self.update_intel)

        # Detect MASS KICK (Potential Netsplit)
        if RE_KICK.search(line):
            now = time.time()
            self.kick_counter = [t for t in self.kick_counter if now - t < 5]
            self.kick_counter.append(now)
            
            if len(self.kick_counter) >= 3 and live and (now - self.last_netsplit_alert > 60):
                self.last_netsplit_alert = now
                self.log("PARSER: MASS KICK DETECTED! (Possible Netsplit)")
                self.send_ntfy("NETSPLIT ALERT", "Mass Kicks Detected! Queue stability risk.", prio_netsplit)
                self.status_bar.config(bg="orange", fg="black")
                self.status_var.set("ALERT: MASS KICK EVENT DETECTED")

        if RE_NETSPLIT.search(line):
            self.netsplit_count += 1
            self.ns_var.set(str(self.netsplit_count))
            if live and (time.time() - self.last_netsplit_alert > 60):
                self.last_netsplit_alert = time.time()
                self.log("PARSER: Netsplit detected! Sending Alert...")
                self.send_ntfy("NETSPLIT ALERT", "A Netsplit just occurred! Check your queue status.", prio_netsplit)
            elif live:
                self.log("PARSER: Netsplit detected (Alert Cooldown)")

        if self.user_nick and self.user_nick.lower() in line_lower:
            if f"<{self.user_nick.lower()}>" not in line_lower:
                if live:
                    self.log("!!! MENTION DETECTED !!!")
                    self.send_ntfy("MENTION ALERT", f"You were mentioned in {self.client_title}", prio_mention)
                    self.after(0, self.flash_alert)

    def start_monitoring(self):
        def monitor():
            self.log("MONITOR: Active")
            while True:
                time.sleep(1) 
                try:
                    # Clipboard Monitor
                    try:
                        curr_clip = self.clipboard_get()
                        # OPTIMIZATION: Check length to prevent freeze on large copy
                        if len(curr_clip) < 500:
                            if curr_clip != self.last_clipboard:
                                if "speedtest.net/result/" in curr_clip:
                                    self.last_clipboard = curr_clip
                                    # Thread-safe GUI update
                                    self.after(0, lambda c=curr_clip: self._update_link_ui(c))
                    except Exception: pass

                    # ... (rest of monitoring loop) ...
                    if int(time.time()) % 30 == 0:
                        self.after(0, self.update_intel)
                        rate = self.db.get_velocity(3)
                        self.velocity_var.set(f"{rate}/hr")
                        if self.current_rank != 999 and rate > 0.1:
                            hours = self.current_rank / rate
                            h, m = divmod(int(hours*60), 60)
                            self.eta_var.set(f"~{h}h {m}m")
                        else:
                            self.eta_var.set("Stalled")

                    prio_top5 = self.config.get("prio_top5", "high")
                    if self.current_rank <= 5:
                        self.pos_lbl.config(fg="#ff0000")
                        self.status_bar.config(bg="red", fg="white")
                        if not self.top5_alert_sent:
                            self.send_ntfy("Top 5 Alert", "You are in the Top 5! Get ready.", prio_top5)
                            self.top5_alert_sent = True
                    else:
                        self.pos_lbl.config(fg="#00ff00")
                        if "ALERT" not in self.status_var.get():
                             self.status_bar.config(bg="#333", fg="white")
                        self.top5_alert_sent = False

                except Exception: pass
        threading.Thread(target=monitor, daemon=True).start()

    def _update_link_ui(self, url):
        self.link_entry.delete(0, tk.END)
        self.link_entry.insert(0, url)
        self.save_manual_link()
        self.log("CLIPBOARD: Auto-captured Speedtest Link")

if __name__ == "__main__":
    app = UniversalHUD()
    app.mainloop()
