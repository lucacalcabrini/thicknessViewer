# -*- coding: utf-8 -*-
"""
ThicknessProfiler DB Analyzer  v1.0.0
Analizzatore per file .db TIA Portal del FB_ControlloSpessoreAlVolo.

Caratteristiche:
  - Load/parse file .db TIA Portal dal DB istanza Fb935_ControlloSpessoreAlVolo
  - Visualizzazione profilo spessore + baseline + delta (1000 celle)
  - Statistiche passata (medio, max, delta) con semaforo Ok/NOK
  - PLC Reader: lettura diretta DB via Snap7 (S7-1500/1200)
  - Auto-Export: polling automatico con salvataggio .db + SQLite
  - History: browser file .db acquisiti, confronto fra passate
  - Tema dark gemello di WeldDetector

Requisiti: pip install matplotlib numpy
Opzionale: pip install python-snap7  (per tab PLC Reader)
Build EXE:  pyinstaller --onefile --windowed thickness_viewer.py

Note PLC:
  Il FB usa S7_Optimized_Access := 'TRUE'. Per leggere via Snap7:
    Opzione A (consigliata): creare un DB "shadow" non-ottimizzato che
                             riceve i dati tramite MOVE dal DB istanza.
    Opzione B: impostare S7_Optimized_Access := 'FALSE' sul FB (impatta
               le prestazioni con array 1000 celle).
"""

APP_VERSION = "1.0.0"
APP_BUILD   = "2026-04-20"
APP_RELEASE = f"v{APP_VERSION} build {APP_BUILD}"

# ── Nascondi console CMD su Windows ──────────────────────
import sys
if sys.platform == "win32":
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass

# ── Pulizia processi: uccide tutto il sottoalbero alla chiusura ──
import os, signal, atexit

_MAIN_PID = os.getpid()

def _kill_process_tree():
    if os.getpid() != _MAIN_PID:
        return
    try:
        if sys.platform == "win32":
            import subprocess
            CREATE_NO_WINDOW = 0x08000000
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(_MAIN_PID)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
        else:
            pgid = os.getpgid(_MAIN_PID)
            os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try: os.kill(_MAIN_PID, signal.SIGKILL)
        except Exception: pass

atexit.register(_kill_process_tree)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re
import datetime
import time
import struct
import sqlite3
import glob
import json

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import numpy as np

# ── SNAP7 (opzionale) ──────────
SNAP7_AVAILABLE = False
try:
    import snap7
    SNAP7_AVAILABLE = True
except ImportError:
    pass


# ══════════════════════════════════════════════════════════════════
#  PARSER FILE .DB TIA PORTAL
# ══════════════════════════════════════════════════════════════════

_RE_ARRAY_VAL  = re.compile(
    r'([\w.]+)\[(\d+)\]\s*:=\s*([+-]?[\d]*\.?[\d]+(?:[eE][+-]?\d+)?)\s*;')
_RE_SCALAR_VAL = re.compile(r'^[ \t]*([\w.]+)\s*:=\s*([^;]+?)\s*;', re.MULTILINE)
_RE_BEGIN      = re.compile(r'\bBEGIN\b', re.IGNORECASE)


def _parse_db_body(text: str, result: dict) -> dict:
    """Popola result da testo .db (dopo BEGIN)."""
    begin_match = _RE_BEGIN.search(text)
    if not begin_match:
        raise ValueError("Blocco BEGIN non trovato nel file .db")
    body = text[begin_match.end():]

    arrays_raw: dict = {}
    for m in _RE_ARRAY_VAL.finditer(body):
        name, idx, val = m.group(1), int(m.group(2)), float(m.group(3))
        arrays_raw.setdefault(name, {})[idx] = val
    for name, idx_dict in arrays_raw.items():
        max_idx = max(idx_dict.keys())
        result["arrays"][name] = [idx_dict.get(i, float("nan")) for i in range(max_idx + 1)]

    array_names = set(arrays_raw.keys())
    for m in _RE_SCALAR_VAL.finditer(body):
        name, raw_val = m.group(1), m.group(2).strip()
        if name in array_names:
            continue
        # Scalare "Dati[i].Spessore" è un array struct: skippa
        if '[' in name and ']' in name:
            continue
        rv_up = raw_val.upper()
        try:
            if rv_up == "TRUE":    result["scalars"][name] = True
            elif rv_up == "FALSE": result["scalars"][name] = False
            else:                  result["scalars"][name] = float(raw_val)
        except ValueError:
            result["scalars"][name] = raw_val
    return result


def parse_db_file(filepath: str) -> dict:
    """Legge e analizza un file .db TIA Portal da disco."""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    result = {"scalars": {}, "arrays": {}, "raw_text": text,
              "filename": os.path.basename(filepath),
              "filepath": filepath,
              "loaded_at": datetime.datetime.now()}
    return _parse_db_body(text, result)


def parse_db_file_from_text(text: str, filename: str = "PLC_direct.db") -> dict:
    result = {"scalars": {}, "arrays": {}, "raw_text": text,
              "filename": filename,
              "loaded_at": datetime.datetime.now()}
    return _parse_db_body(text, result)


# ══════════════════════════════════════════════════════════════════
#  PLC OFFSET MAP - FB_ControlloSpessoreAlVolo (non-ottimizzato)
# ══════════════════════════════════════════════════════════════════
# NOTA: Gli offset sono calcolati per DB non-ottimizzato con regole di
# allineamento standard S7. Se il DB è ottimizzato o la struttura UDT
# cambia, gli offset vanno ricalcolati. Preferire in tal caso il flusso
# .db file (export/import da TIA Portal).

ARRAY_SIZE = 1000   # aProfiloSpessore, aProfiloDelta, aBaseline

PLC_REAL_SIZE  = 4
PLC_LREAL_SIZE = 8
PLC_INT_SIZE   = 2


def plc_build_offset_map():
    """Mappa offset per il DB istanza FB_ControlloSpessoreAlVolo (non-ottim).

    ATTENZIONE: è una stima basata sulla struttura SCL. Per DB non-ottimizzati
    reali, verificare gli offset in TIA Portal (vista "indirizzo assoluto").
    """
    entries = []
    off = 0

    def align(n):
        nonlocal off
        if off % n: off += (n - off % n)

    def real(name):
        nonlocal off
        align(2)
        entries.append((name, off, 'real', PLC_REAL_SIZE))
        off += PLC_REAL_SIZE

    def lreal(name):
        nonlocal off
        align(2)
        entries.append((name, off, 'lreal', PLC_LREAL_SIZE))
        off += PLC_LREAL_SIZE

    def sint(name):
        nonlocal off
        align(2)
        entries.append((name, off, 'int', PLC_INT_SIZE))
        off += PLC_INT_SIZE

    def bools(names):
        nonlocal off
        align(2)
        base = off
        for i, n in enumerate(names):
            entries.append((n, base + i // 8, 'bool', i % 8))
        off = base + (len(names) - 1) // 8 + 1
        align(2)

    def arr_real(name, n):
        nonlocal off
        align(2)
        entries.append((name, off, 'array_real', n))
        off += n * PLC_REAL_SIZE

    # ─── VAR_INPUT ──────────────────────────────────────────────────
    # I_ParametriCntrolloSpessore (UDT)
    real('I_ParametriCntrolloSpessore.PosizioneCentroVentosa')
    real('I_ParametriCntrolloSpessore.RangeControllo')
    real('I_ParametriCntrolloSpessore.RangeRallenta')
    real('I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme')
    real('I_ParametriCntrolloSpessore.SpessoreMassimo')
    lreal('I_ParametriCntrolloSpessore.OvrTrasfertPerpassaggio')
    bools(['I_ParametriCntrolloSpessore.DisabilitaControllo',
           'I_ParametriCntrolloSpessore.AbilitaTaratura'])
    real('I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento')
    # Altri input scalari
    real('I_Spessore_mm')
    bools(['I_InvertiLettura', 'I_PiecePresence'])
    real('I_ActPosition')
    real('I_ActSpeed')
    sint('I_DirLavoro')

    # ─── VAR_OUTPUT ─────────────────────────────────────────────────
    bools(['O_SpessoreOk', 'O_SpessoreNOK', 'O_AlmControlloDisattivo',
           'O_TaraturaAttiva', 'O_TaraturaCompletata'])
    real('O_SpessoreMedio')
    real('O_SpessoreMax')
    real('O_DeltaMedio')
    real('O_DeltaMax')
    sint('O_nCelleProfilo')

    # ─── VAR_IN_OUT (puntatori) ────────────────────────────────────
    # Per DB non-ottimizzato: 6 byte ciascuno
    align(2)
    off += 6 * 2   # IO_OvrAuto + IO_OvrMan

    # ─── VAR RETAIN (Pinza struct) ─────────────────────────────────
    bools(['Pinza.InZonaControllo', 'Pinza.InZonaRallenta'])
    lreal('Pinza.OvrAutoOld')
    lreal('Pinza.OvrManOld')
    real('Pinza.nLettureConsecutive')

    # ─── VAR RETAIN (baseline) ─────────────────────────────────────
    arr_real('aBaseline', ARRAY_SIZE)
    bools(['baselineValida'])

    # ─── VAR (R_TRIG/F_TRIG + scalari + buffer Dati) ───────────────
    # Ogni R_TRIG/F_TRIG occupa 2 byte (CLK + Q packed)
    off += 5 * 2   # Fp, Fn, FpSlowing, FnSlowing, FpTaratura
    bools(['DirOk', 'Ripeti'])
    sint('index')
    sint('AppMinIndexSurce')
    sint('AppMaxIndexSurce')
    sint('AppMaxIndex')
    # Dati[0..100] struct: 3 Real = 12 byte ciascuno
    off += 101 * 12
    bools(['taraturaInCorso'])
    arr_real('aSomRaw', ARRAY_SIZE)
    # aNraw[0..999] Int = 2000 byte
    align(2)
    off += ARRAY_SIZE * PLC_INT_SIZE
    arr_real('aSomCal', ARRAY_SIZE)
    align(2)
    off += ARRAY_SIZE * PLC_INT_SIZE   # aNcal
    arr_real('aProfiloSpessore', ARRAY_SIZE)
    arr_real('aProfiloDelta', ARRAY_SIZE)

    # ─── VAR RETAIN (risultati scalari) ────────────────────────────
    bools(['AppSpessoreOk', 'AppSpessoreNok'])
    real('AppSpessoreMedio')
    real('AppSpessoreMax')
    real('AppDeltaMedio')
    real('AppDeltaMax')
    sint('AppNcelleProfilo')

    return entries, off


def plc_decode_real(d, o):  return struct.unpack('>f', d[o:o+4])[0]
def plc_decode_lreal(d, o): return struct.unpack('>d', d[o:o+8])[0]
def plc_decode_int(d, o):   return struct.unpack('>h', d[o:o+2])[0]
def plc_decode_bool(d, o, b): return bool(d[o] & (1 << b))


def plc_decode_array_real(d, o, n):
    return list(struct.unpack(f'>{n}f', d[o:o + n * 4]))


def plc_decode_db(raw, offset_map):
    """Decodifica byte grezzi usando la mappa offset."""
    result = {'scalars': {}, 'arrays': {}}
    for name, off, dtype, sz in offset_map:
        try:
            if dtype == 'real':    result['scalars'][name] = plc_decode_real(raw, off)
            elif dtype == 'lreal': result['scalars'][name] = plc_decode_lreal(raw, off)
            elif dtype == 'int':   result['scalars'][name] = plc_decode_int(raw, off)
            elif dtype == 'bool':  result['scalars'][name] = plc_decode_bool(raw, off, sz)
            elif dtype == 'array_real':
                result['arrays'][name] = plc_decode_array_real(raw, off, sz)
        except (struct.error, IndexError):
            if dtype == 'array_real': result['arrays'][name] = [0.0] * sz
            elif dtype == 'bool':     result['scalars'][name] = False
            elif dtype == 'int':      result['scalars'][name] = 0
            else:                     result['scalars'][name] = 0.0
    return result


def plc_generate_db_text(decoded, db_name="ControlloSpessore_Export"):
    """Genera file .db in formato TIA Portal dall'output del decoder."""
    lines = [
        f'DATA_BLOCK "{db_name}"',
        "{ S7_Optimized_Access := 'FALSE' }",
        "VERSION : 0.1",
        "NON_RETAIN",
        "   // Esportazione automatica da ThicknessProfiler",
        "BEGIN",
    ]
    # Scalari
    for name, val in decoded['scalars'].items():
        if isinstance(val, bool):
            lines.append(f"   {name} := {'TRUE' if val else 'FALSE'};")
        elif isinstance(val, int):
            lines.append(f"   {name} := {val};")
        else:
            lines.append(f"   {name} := {val:.7e};")
    # Array
    for name, arr in decoded['arrays'].items():
        for i, v in enumerate(arr):
            lines.append(f"   {name}[{i}] := {v:.7e};")
    lines.append("END_DATA_BLOCK")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  PLC READER Snap7
# ══════════════════════════════════════════════════════════════════

class PLCReader:
    """Gestisce connessione snap7 e lettura DB da PLC S7-1500/1200."""
    def __init__(self, ip, rack=0, slot=1):
        if not SNAP7_AVAILABLE:
            raise ImportError("python-snap7 non installato!\npip install python-snap7")
        self.ip = ip; self.rack = rack; self.slot = slot
        self.client = snap7.client.Client()

    def connect(self):
        self.client.connect(self.ip, self.rack, self.slot)
        if not self.client.get_connected():
            raise ConnectionError(f"Impossibile connettersi a {self.ip}")
        info = self.client.get_cpu_info()
        cpu = info.ModuleTypeName.decode().strip()
        pdu = self.client.get_pdu_length()
        return cpu, pdu

    def disconnect(self):
        if self.client.get_connected():
            self.client.disconnect()

    def read_db_raw(self, db_number, total_size, chunk=400, callback=None):
        data = bytearray(total_size); off = 0; reads = 0
        while off < total_size:
            sz = min(chunk, total_size - off)
            try:
                data[off:off+sz] = self.client.db_read(db_number, off, sz)
            except Exception as e:
                raise RuntimeError(
                    f"Errore lettura DB{db_number} @{off}: {e}\n"
                    f"Verifica: 1) DB esiste  2) S7_Optimized_Access=FALSE  "
                    f"3) PUT/GET abilitato sulla CPU")
            off += sz; reads += 1
            if callback and reads % 20 == 0:
                callback(off * 100 // total_size)
        return data

    def read_bytes(self, db_number, start, size):
        """Lettura mirata di N byte a partire da offset start."""
        return self.client.db_read(db_number, start, size)


# ══════════════════════════════════════════════════════════════════
#  SQLITE HELPER
# ══════════════════════════════════════════════════════════════════

def sqlite_init(db_path):
    """Inizializza archivio SQLite per passate."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS acquisizioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            filename TEXT,
            db_number INTEGER,
            spessore_medio REAL,
            spessore_max REAL,
            delta_medio REAL,
            delta_max REAL,
            n_celle INTEGER,
            spessore_ok INTEGER,
            spessore_nok INTEGER,
            taratura INTEGER,
            raw_db TEXT
        )
    """)
    con.commit()
    return con


def sqlite_insert(con, decoded, filename, db_number=0, is_taratura=False):
    sc = decoded['scalars']
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw_db = plc_generate_db_text(decoded, db_name=f"DB{db_number}_{ts}")
    cur = con.execute("""
        INSERT INTO acquisizioni
        (timestamp, filename, db_number, spessore_medio, spessore_max,
         delta_medio, delta_max, n_celle, spessore_ok, spessore_nok,
         taratura, raw_db)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ts, filename, db_number,
        float(sc.get('AppSpessoreMedio', sc.get('O_SpessoreMedio', 0.0))),
        float(sc.get('AppSpessoreMax',   sc.get('O_SpessoreMax',   0.0))),
        float(sc.get('AppDeltaMedio',    sc.get('O_DeltaMedio',    0.0))),
        float(sc.get('AppDeltaMax',      sc.get('O_DeltaMax',      0.0))),
        int(sc.get('AppNcelleProfilo',   sc.get('O_nCelleProfilo', 0))),
        int(bool(sc.get('AppSpessoreOk', sc.get('O_SpessoreOk',    False)))),
        int(bool(sc.get('AppSpessoreNok',sc.get('O_SpessoreNOK',   False)))),
        int(bool(is_taratura)),
        raw_db
    ))
    con.commit()
    return cur.lastrowid


# ══════════════════════════════════════════════════════════════════
#  PALETTE (matching weld_viewer)
# ══════════════════════════════════════════════════════════════════

DARK_BG   = "#000000"
PANEL_BG  = "#0d1117"
BORDER_CLR = "#484f58"
ACCENT    = "#79c0ff"
OK_CLR    = "#56d364"
WARN_CLR  = "#e3b341"
ERR_CLR   = "#FF6B6B"
TEXT_CLR  = "#f0f6fc"
MUTED_CLR = "#b1bac4"
ENTRY_BG  = "#161b22"
PROFILE_CLR  = "#79c0ff"   # curva profilo spessore
BASELINE_CLR = "#e3b341"   # baseline
DELTA_CLR    = "#ff9070"   # delta
THRESHOLD_CLR= "#ff6e85"   # soglia doppio spessore
PLC_CLR   = "#f0883e"
AUTOEXP_CLR = "#56d364"


# ══════════════════════════════════════════════════════════════════
#  APPLICAZIONE
# ══════════════════════════════════════════════════════════════════

class ThicknessApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"◈ Thickness Profiler  {APP_RELEASE}  —  FB935 v0.2")
        self.geometry("1420x900")
        self.minsize(1120, 700)
        self.configure(bg=DARK_BG)

        self.db_data = None               # dati correnti caricati
        self._db_history = []             # lista di dict, ultime passate
        self._settings_file = os.path.join(
            os.path.expanduser('~'), 'ThicknessProfiler', 'settings.json')
        self._settings = self._load_settings()

        # Auto-export state
        self._autoexp_running = False
        self._autoexp_timer_id = None
        self._autoexp_client = None
        self._autoexp_count_ok = 0
        self._autoexp_count_nok = 0
        self._autoexp_count_tar = 0
        self._autoexp_sql_con = None
        self._autoexp_prev_sentinel = None

        self._style()
        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._startup_checks)

    # ─── SETTINGS (JSON) ──────────────────────────────────────
    def _load_settings(self):
        try:
            if os.path.isfile(self._settings_file):
                with open(self._settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(self._settings_file), exist_ok=True)
            data = {
                'plc_ip': self._pv_plc_ip.get() if hasattr(self, '_pv_plc_ip') else '192.168.0.1',
                'plc_rack': self._pv_plc_rack.get() if hasattr(self, '_pv_plc_rack') else '0',
                'plc_slot': self._pv_plc_slot.get() if hasattr(self, '_pv_plc_slot') else '1',
                'plc_db':  self._pv_plc_db.get()  if hasattr(self, '_pv_plc_db') else '100',
                'autoexp_path_ok':  self._pv_autoexp_path.get() if hasattr(self, '_pv_autoexp_path') else '',
                'autoexp_path_nok': self._pv_autoexp_path_rej.get() if hasattr(self, '_pv_autoexp_path_rej') else '',
                'sqlite_path':      self._pv_sql_path.get() if hasattr(self, '_pv_sql_path') else '',
            }
            with open(self._settings_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.app_log(f"Errore salvataggio settings: {e}", "warn")

    def _startup_checks(self):
        missing = []
        try: import numpy
        except ImportError: missing.append("numpy")
        try: import matplotlib
        except ImportError: missing.append("matplotlib")
        if missing:
            messagebox.showwarning("Librerie mancanti",
                f"Installare: {', '.join(missing)}\n\npip install {' '.join(missing)}")

    def _on_close(self):
        if self._autoexp_running:
            self._autoexp_stop()
        self._save_settings()
        try: self.destroy()
        except Exception: pass
        _kill_process_tree()

    # ─── STILE (matching weld_viewer) ──────────────────────────
    def _style(self):
        st = ttk.Style(self); st.theme_use("clam")
        base = dict(background=DARK_BG, foreground=TEXT_CLR, fieldbackground=ENTRY_BG,
                    troughcolor=PANEL_BG, bordercolor=BORDER_CLR,
                    lightcolor=BORDER_CLR, darkcolor=BORDER_CLR, font=("Consolas", 10))
        st.configure(".", **base)
        st.configure("TFrame", background=DARK_BG)
        st.configure("TLabel", background=DARK_BG, foreground=TEXT_CLR)
        st.configure("Muted.TLabel", background=DARK_BG, foreground=MUTED_CLR, font=("Consolas", 9))
        st.configure("Title.TLabel", background=DARK_BG, foreground=ACCENT, font=("Consolas", 12, "bold"))
        st.configure("Result.TLabel", background=PANEL_BG, foreground=ACCENT, font=("Consolas", 10))
        st.configure("Big.TLabel", background=DARK_BG, foreground=ACCENT, font=("Consolas", 14, "bold"))
        st.configure("Ok.TLabel", background=DARK_BG, foreground=OK_CLR, font=("Consolas", 11, "bold"))
        st.configure("Nok.TLabel", background=DARK_BG, foreground=ERR_CLR, font=("Consolas", 11, "bold"))
        st.configure("Warn.TLabel", background=DARK_BG, foreground=WARN_CLR, font=("Consolas", 11, "bold"))

        for name, bg, fg in [("Accent", ACCENT, DARK_BG),
                              ("Plc", PLC_CLR, DARK_BG),
                              ("Auto", AUTOEXP_CLR, DARK_BG),
                              ("Err", ERR_CLR, DARK_BG)]:
            st.configure(f"{name}.TButton", background=bg, foreground=fg,
                         font=("Consolas", 10, "bold"), padding=(10, 5))

        st.configure("TButton", background=ENTRY_BG, foreground=TEXT_CLR,
                     bordercolor=BORDER_CLR, padding=(8, 4))
        st.map("TButton", background=[("active", ACCENT)],
               foreground=[("active", DARK_BG)])

        st.configure("TNotebook", background=DARK_BG, bordercolor=BORDER_CLR)
        st.configure("TNotebook.Tab", background=PANEL_BG, foreground=MUTED_CLR,
                     padding=(14, 5), bordercolor=BORDER_CLR)
        st.map("TNotebook.Tab", background=[("selected", DARK_BG)],
               foreground=[("selected", ACCENT)])

        st.configure("TEntry", fieldbackground=ENTRY_BG, foreground=TEXT_CLR,
                     insertcolor=TEXT_CLR)
        st.configure("TLabelframe", background=DARK_BG, foreground=MUTED_CLR,
                     bordercolor=BORDER_CLR)
        st.configure("TLabelframe.Label", background=DARK_BG, foreground=MUTED_CLR)

        st.configure("Treeview", background=ENTRY_BG, foreground=TEXT_CLR,
                     fieldbackground=ENTRY_BG, rowheight=22, bordercolor=BORDER_CLR,
                     font=("Consolas", 9))
        st.configure("Treeview.Heading", background=PANEL_BG, foreground=ACCENT,
                     relief="flat", font=("Consolas", 9, "bold"))
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", DARK_BG)])

        st.configure("TCheckbutton", background=DARK_BG, foreground=TEXT_CLR,
                     indicatorcolor="#1f6feb", indicatorrelief="flat")
        st.configure("TRadiobutton", background=DARK_BG, foreground=TEXT_CLR)
        st.configure("TScrollbar", background=PANEL_BG, troughcolor=DARK_BG,
                     arrowcolor=MUTED_CLR, bordercolor=BORDER_CLR)
        st.configure("TProgressbar", troughcolor=PANEL_BG, background=ACCENT,
                     bordercolor=BORDER_CLR)
        st.configure("TCombobox", fieldbackground=ENTRY_BG, background=ENTRY_BG,
                     foreground=TEXT_CLR, arrowcolor=TEXT_CLR,
                     bordercolor=BORDER_CLR)
        st.map("TCombobox",
               fieldbackground=[("readonly", ENTRY_BG), ("disabled", PANEL_BG)],
               foreground=[("readonly", TEXT_CLR)])

    # ─── LAYOUT PRINCIPALE ────────────────────────────────────
    def _build_ui(self):
        # Barra superiore
        top = ttk.Frame(self); top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(top, text="◈ THICKNESS PROFILER",
                  style="Title.TLabel").pack(side="left")
        tk.Label(top, text=APP_RELEASE, font=("Consolas", 9),
                 fg="#58a6ff", bg=DARK_BG, padx=8).pack(side="left")

        ttk.Button(top, text="📁 Apri file .db...",
                   style="Accent.TButton",
                   command=self._open_file).pack(side="right", padx=2)
        ttk.Button(top, text="💾 Salva .db come...",
                   command=self._save_as_db).pack(side="right", padx=2)

        # Pannello principale: sinistra (info/params) + destra (tabs)
        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=10, pady=8)

        self._left_panel = ttk.Frame(main, width=340)
        self._left_panel.pack_propagate(False)
        main.add(self._left_panel, weight=0)

        right_panel = ttk.Frame(main)
        main.add(right_panel, weight=1)

        self._build_left(self._left_panel)
        self._build_right(right_panel)

        # Barra inferiore: log applicazione
        bot = ttk.Frame(self); bot.pack(fill="x", padx=10, pady=(0, 6))
        self._lbl_status = tk.Label(bot, text="Pronto.",
            bg=DARK_BG, fg=MUTED_CLR, font=("Consolas", 9), anchor="w")
        self._lbl_status.pack(side="left", fill="x", expand=True)
        self._lbl_file = tk.Label(bot, text="Nessun file caricato",
            bg=DARK_BG, fg=ACCENT, font=("Consolas", 9, "bold"), anchor="e")
        self._lbl_file.pack(side="right")

    # ─── PANNELLO SINISTRO (info + risultati + parametri) ─────
    def _build_left(self, parent):
        # Scroll area
        canv = tk.Canvas(parent, bg=DARK_BG, highlightthickness=0)
        scr  = ttk.Scrollbar(parent, orient="vertical", command=canv.yview)
        inner = ttk.Frame(canv)
        inner.bind("<Configure>", lambda e: canv.configure(scrollregion=canv.bbox("all")))
        wid = canv.create_window((0, 0), window=inner, anchor="nw")
        canv.configure(yscrollcommand=scr.set)
        canv.bind("<Configure>", lambda e: canv.itemconfigure(wid, width=e.width))
        canv.bind("<MouseWheel>", lambda e: canv.yview_scroll(int(-1*(e.delta/120)), "units"))
        scr.pack(side="right", fill="y")
        canv.pack(side="left", fill="both", expand=True)

        # ── Box RISULTATO ────────────────────────────────────
        box_res = ttk.LabelFrame(inner, text="  RISULTATO ULTIMA PASSATA  ",
                                 padding=8)
        box_res.pack(fill="x", padx=4, pady=4)

        self._pv_verdict = tk.StringVar(value="—")
        tk.Label(box_res, textvariable=self._pv_verdict,
                 bg=DARK_BG, font=("Consolas", 18, "bold"),
                 fg=MUTED_CLR).pack(pady=4)

        grid = ttk.Frame(box_res); grid.pack(fill="x", pady=2)
        self._pv_spess_med = tk.StringVar(value="—")
        self._pv_spess_max = tk.StringVar(value="—")
        self._pv_delta_med = tk.StringVar(value="—")
        self._pv_delta_max = tk.StringVar(value="—")
        self._pv_n_celle   = tk.StringVar(value="—")
        for r, (lbl, var) in enumerate([
                ("Spessore medio",  self._pv_spess_med),
                ("Spessore max",    self._pv_spess_max),
                ("Delta medio",     self._pv_delta_med),
                ("Delta max",       self._pv_delta_max),
                ("Celle campionate",self._pv_n_celle),
                ]):
            tk.Label(grid, text=lbl, bg=DARK_BG, fg=MUTED_CLR,
                     font=("Consolas", 9), width=18, anchor="w"
                     ).grid(row=r, column=0, sticky="w", padx=2, pady=1)
            tk.Label(grid, textvariable=var, bg=DARK_BG, fg=ACCENT,
                     font=("Consolas", 10, "bold"), width=12, anchor="e"
                     ).grid(row=r, column=1, sticky="e", padx=2, pady=1)

        # ── Box STATO TARATURA ──────────────────────────────
        box_tar = ttk.LabelFrame(inner, text="  TARATURA  ", padding=8)
        box_tar.pack(fill="x", padx=4, pady=4)
        self._pv_tar_stato = tk.StringVar(value="—")
        tk.Label(box_tar, textvariable=self._pv_tar_stato,
                 bg=DARK_BG, font=("Consolas", 11, "bold"),
                 fg=MUTED_CLR).pack(pady=2)
        self._pv_tar_rif = tk.StringVar(value="Riferimento: — mm")
        tk.Label(box_tar, textvariable=self._pv_tar_rif,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9)).pack()

        # ── Box PARAMETRI UDT ───────────────────────────────
        box_par = ttk.LabelFrame(inner, text="  PARAMETRI UDT  ", padding=6)
        box_par.pack(fill="x", padx=4, pady=4)

        self._pv_params = {}
        for key, label in [
                ('PosizioneCentroVentosa',    'Centro ventosa'),
                ('RangeControllo',            'Range controllo'),
                ('RangeRallenta',             'Range rallenta'),
                ('SpessoreMassimo',           'Soglia max'),
                ('nLettureConsecutiveAllarme','N. consecutive'),
                ('OvrTrasfertPerpassaggio',   'Override %'),
                ('SpessoreDiscoRiferimento',  'Disco riferimento'),
                ('DisabilitaControllo',       'Disabilitato'),
                ('AbilitaTaratura',           'Tarat. abilitata'),
                ]:
            row = ttk.Frame(box_par); row.pack(fill="x", pady=1)
            tk.Label(row, text=label, bg=DARK_BG, fg=MUTED_CLR,
                     font=("Consolas", 9), width=18, anchor="w").pack(side="left")
            sv = tk.StringVar(value="—")
            tk.Label(row, textvariable=sv, bg=DARK_BG, fg=TEXT_CLR,
                     font=("Consolas", 9), anchor="e").pack(side="right")
            self._pv_params[key] = sv

        # ── Box STATO PINZA ─────────────────────────────────
        box_pz = ttk.LabelFrame(inner, text="  STATO PINZA  ", padding=6)
        box_pz.pack(fill="x", padx=4, pady=4)
        self._pv_piece = tk.StringVar(value="—")
        self._pv_zona_ctrl = tk.StringVar(value="—")
        self._pv_zona_rall = tk.StringVar(value="—")
        for lbl, var in [("Pezzo presente:", self._pv_piece),
                         ("In zona ctrl:", self._pv_zona_ctrl),
                         ("In zona rall:", self._pv_zona_rall)]:
            row = ttk.Frame(box_pz); row.pack(fill="x", pady=1)
            tk.Label(row, text=lbl, bg=DARK_BG, fg=MUTED_CLR,
                     font=("Consolas", 9), width=16, anchor="w").pack(side="left")
            tk.Label(row, textvariable=var, bg=DARK_BG, fg=TEXT_CLR,
                     font=("Consolas", 9, "bold"), anchor="e").pack(side="right")

    # ─── PANNELLO DESTRO (tabs) ────────────────────────────────
    def _build_right(self, parent):
        self._nb = ttk.Notebook(parent)
        self._nb.pack(fill="both", expand=True)

        # Tab 1: Viewer principale (profilo + baseline + delta)
        t_viewer = ttk.Frame(self._nb); self._nb.add(t_viewer, text="  📊 Profilo  ")
        self._build_viewer_tab(t_viewer)

        # Tab 2: Delta zoom
        t_delta = ttk.Frame(self._nb); self._nb.add(t_delta, text="  📈 Delta  ")
        self._build_delta_tab(t_delta)

        # Tab 3: PLC Reader
        t_plc = ttk.Frame(self._nb); self._nb.add(t_plc, text="  🔌 PLC Reader  ")
        self._build_plc_tab(t_plc)

        # Tab 4: Auto-Export
        t_auto = ttk.Frame(self._nb); self._nb.add(t_auto, text="  ⚡ Auto-Export  ")
        self._build_autoexp_tab(t_auto)

        # Tab 5: History
        t_hist = ttk.Frame(self._nb); self._nb.add(t_hist, text="  📚 History  ")
        self._build_history_tab(t_hist)

        # Tab 6: Impostazioni
        t_cfg = ttk.Frame(self._nb); self._nb.add(t_cfg, text="  ⚙ Impostazioni  ")
        self._build_settings_tab(t_cfg)

    # ══════════════════════════════════════════════════════════
    #  TAB 1 — VIEWER PROFILO
    # ══════════════════════════════════════════════════════════
    def _build_viewer_tab(self, parent):
        # Toolbar interna
        bar = ttk.Frame(parent); bar.pack(fill="x", padx=4, pady=4)
        self._pv_show_profile = tk.BooleanVar(value=True)
        self._pv_show_baseline = tk.BooleanVar(value=True)
        self._pv_show_delta = tk.BooleanVar(value=True)
        self._pv_show_threshold = tk.BooleanVar(value=True)
        self._pv_show_samples = tk.BooleanVar(value=False)

        for var, text in [
                (self._pv_show_profile,   "Profilo"),
                (self._pv_show_baseline,  "Baseline"),
                (self._pv_show_delta,     "Delta"),
                (self._pv_show_threshold, "Soglia"),
                (self._pv_show_samples,   "Campioni buffer"),
                ]:
            tk.Checkbutton(bar, text=text, variable=var,
                bg=DARK_BG, fg=TEXT_CLR, selectcolor="#1f6feb",
                activebackground=DARK_BG, font=("Consolas", 9),
                command=self._draw_viewer).pack(side="left", padx=4)

        ttk.Button(bar, text="🔄 Refresh",
                   command=self._draw_viewer).pack(side="right", padx=2)
        ttk.Button(bar, text="💾 PNG",
                   command=lambda: self._save_plot(self.fig_viewer)
                   ).pack(side="right", padx=2)

        # Matplotlib figure
        self.fig_viewer = Figure(figsize=(10, 6), dpi=95, facecolor=DARK_BG)
        self.ax_viewer = self.fig_viewer.add_subplot(111, facecolor=PANEL_BG)
        self._stylize_ax(self.ax_viewer)

        canv = FigureCanvasTkAgg(self.fig_viewer, parent)
        canv.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)
        self._viewer_canvas = canv

        tb_frm = ttk.Frame(parent); tb_frm.pack(fill="x")
        tb = NavigationToolbar2Tk(canv, tb_frm)
        tb.config(background=DARK_BG)
        for btn in tb.winfo_children():
            btn.config(background=DARK_BG)
        tb.update()

    def _stylize_ax(self, ax):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=TEXT_CLR, which='both')
        for spine in ax.spines.values():
            spine.set_color(BORDER_CLR)
        ax.grid(True, alpha=0.15, color=BORDER_CLR)

    def _draw_viewer(self):
        ax = self.ax_viewer
        ax.clear()
        self._stylize_ax(ax)

        if self.db_data is None:
            ax.text(0.5, 0.5, "Nessun dato — carica un file .db o leggi dal PLC",
                    ha='center', va='center', color=MUTED_CLR,
                    fontsize=12, transform=ax.transAxes)
            self._viewer_canvas.draw_idle()
            return

        arrays = self.db_data.get('arrays', {})
        scalars = self.db_data.get('scalars', {})

        # Calcolo asse X in mm reali
        range_ctrl = self._get_scalar(scalars, 'RangeControllo', 'I_ParametriCntrolloSpessore.RangeControllo', default=150.0)
        pos_ctr = self._get_scalar(scalars, 'PosizioneCentroVentosa', 'I_ParametriCntrolloSpessore.PosizioneCentroVentosa', default=630.0)
        soglia  = self._get_scalar(scalars, 'SpessoreMassimo', 'I_ParametriCntrolloSpessore.SpessoreMassimo', default=1.5)
        sp_rif  = self._get_scalar(scalars, 'SpessoreDiscoRiferimento', 'I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento', default=1.0)

        n = ARRAY_SIZE
        x_mm = np.linspace(pos_ctr - range_ctrl, pos_ctr + range_ctrl, n)

        # Profilo spessore (compensato)
        prof = arrays.get('aProfiloSpessore', [])
        if len(prof) >= n and self._pv_show_profile.get():
            p = np.array(prof[:n])
            mask = np.abs(p) > 1e-6    # nasconde celle non campionate a 0
            ax.plot(x_mm[mask], p[mask], color=PROFILE_CLR, lw=1.5,
                    label='Profilo spessore [mm]', zorder=5)

        # Baseline
        bas = arrays.get('aBaseline', [])
        if len(bas) >= n and self._pv_show_baseline.get():
            b = np.array(bas[:n])
            mask = np.abs(b) > 1e-6
            ax.plot(x_mm[mask], b[mask], color=BASELINE_CLR, lw=1.0,
                    ls='--', alpha=0.8, label='Baseline (offset macchina) [mm]')

        # Delta
        dlt = arrays.get('aProfiloDelta', [])
        if len(dlt) >= n and self._pv_show_delta.get():
            d = np.array(dlt[:n])
            mask = np.abs(d) > 1e-6
            ax.plot(x_mm[mask], d[mask], color=DELTA_CLR, lw=1.2,
                    alpha=0.8, label=f'Delta vs riferimento ({sp_rif:.2f}mm)')

        # Soglia doppio spessore (orizzontale)
        if self._pv_show_threshold.get():
            ax.axhline(soglia, color=THRESHOLD_CLR, lw=1.0, ls=':',
                       alpha=0.9, label=f'Soglia doppio spessore ({soglia:.2f}mm)')

        # Campioni buffer temporale Dati[0..100] (opzionale)
        if self._pv_show_samples.get():
            samples_x, samples_y = [], []
            for i in range(101):
                k_sp = f'Dati[{i}].Spessore'
                k_po = f'Dati[{i}].Pos'
                if k_sp in scalars and k_po in scalars:
                    p = scalars[k_po]; s = scalars[k_sp]
                    if abs(p) > 1e-6 or abs(s) > 1e-6:
                        samples_x.append(p); samples_y.append(s)
            if samples_x:
                ax.scatter(samples_x, samples_y, color="#f9c74f",
                           s=18, marker='o', alpha=0.7,
                           label=f'Buffer temporale ({len(samples_x)} pt)',
                           zorder=7)

        ax.set_xlabel("Posizione asse trasfert [mm]", color=TEXT_CLR, fontsize=10)
        ax.set_ylabel("Spessore [mm]", color=TEXT_CLR, fontsize=10)

        title = self.db_data.get('filename', '—')
        ts = self.db_data.get('loaded_at')
        if ts:
            title = f"{title}   •   {ts.strftime('%H:%M:%S')}"
        ax.set_title(title, color=ACCENT, fontsize=10, pad=6)

        # Range zone di lavoro
        ax.axvline(pos_ctr - range_ctrl, color=BORDER_CLR, lw=0.7, ls=':', alpha=0.6)
        ax.axvline(pos_ctr + range_ctrl, color=BORDER_CLR, lw=0.7, ls=':', alpha=0.6)
        ax.axvline(pos_ctr, color=BORDER_CLR, lw=0.5, ls='--', alpha=0.4)

        leg = ax.legend(loc='upper right', fontsize=8, framealpha=0.85,
                        facecolor=PANEL_BG, edgecolor=BORDER_CLR,
                        labelcolor=TEXT_CLR)
        if leg:
            leg.get_frame().set_facecolor(PANEL_BG)

        self.fig_viewer.tight_layout()
        self._viewer_canvas.draw_idle()

    @staticmethod
    def _get_scalar(scalars, *keys, default=0.0):
        for k in keys:
            if k in scalars:
                return float(scalars[k])
        return default

    # ══════════════════════════════════════════════════════════
    #  TAB 2 — DELTA ZOOM
    # ══════════════════════════════════════════════════════════
    def _build_delta_tab(self, parent):
        bar = ttk.Frame(parent); bar.pack(fill="x", padx=4, pady=4)
        ttk.Label(bar, text="Zoom sul delta — evidenzia scostamenti dal riferimento",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="🔄 Refresh", command=self._draw_delta).pack(side="right", padx=2)
        ttk.Button(bar, text="💾 PNG",
                   command=lambda: self._save_plot(self.fig_delta)
                   ).pack(side="right", padx=2)

        self.fig_delta = Figure(figsize=(10, 6), dpi=95, facecolor=DARK_BG)
        self.ax_delta = self.fig_delta.add_subplot(111, facecolor=PANEL_BG)
        self._stylize_ax(self.ax_delta)
        canv = FigureCanvasTkAgg(self.fig_delta, parent)
        canv.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)
        self._delta_canvas = canv

    def _draw_delta(self):
        ax = self.ax_delta
        ax.clear()
        self._stylize_ax(ax)

        if self.db_data is None:
            ax.text(0.5, 0.5, "Nessun dato", ha='center', va='center',
                    color=MUTED_CLR, fontsize=12, transform=ax.transAxes)
            self._delta_canvas.draw_idle()
            return

        arrays = self.db_data.get('arrays', {})
        scalars = self.db_data.get('scalars', {})
        range_ctrl = self._get_scalar(scalars, 'RangeControllo', 'I_ParametriCntrolloSpessore.RangeControllo', default=150.0)
        pos_ctr = self._get_scalar(scalars, 'PosizioneCentroVentosa', 'I_ParametriCntrolloSpessore.PosizioneCentroVentosa', default=630.0)
        sp_rif  = self._get_scalar(scalars, 'SpessoreDiscoRiferimento', 'I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento', default=1.0)
        soglia  = self._get_scalar(scalars, 'SpessoreMassimo', 'I_ParametriCntrolloSpessore.SpessoreMassimo', default=1.5)

        n = ARRAY_SIZE
        x_mm = np.linspace(pos_ctr - range_ctrl, pos_ctr + range_ctrl, n)
        dlt = arrays.get('aProfiloDelta', [])

        if len(dlt) >= n:
            d = np.array(dlt[:n])
            mask = np.abs(d) > 1e-6

            # Fill colorato in base al segno
            x_m = x_mm[mask]; d_m = d[mask]
            ax.fill_between(x_m, 0, d_m, where=(d_m >= 0),
                            color=ERR_CLR, alpha=0.35,
                            label='Eccesso (più spesso del riferimento)')
            ax.fill_between(x_m, 0, d_m, where=(d_m < 0),
                            color=ACCENT, alpha=0.35,
                            label='Difetto (più sottile del riferimento)')
            ax.plot(x_m, d_m, color=DELTA_CLR, lw=1.2)

        ax.axhline(0, color=MUTED_CLR, lw=1.0, ls='-', alpha=0.6)
        # Soglia allarme (soglia - riferimento = eccesso che scatena allarme)
        allarme = soglia - sp_rif
        ax.axhline(allarme, color=THRESHOLD_CLR, lw=1.0, ls=':',
                   alpha=0.9, label=f'Soglia allarme (delta={allarme:.2f}mm)')

        ax.set_xlabel("Posizione asse [mm]", color=TEXT_CLR, fontsize=10)
        ax.set_ylabel("Delta spessore [mm]", color=TEXT_CLR, fontsize=10)
        ax.set_title(f"Delta — riferimento {sp_rif:.3f} mm",
                     color=ACCENT, fontsize=10, pad=6)

        leg = ax.legend(loc='upper right', fontsize=8, framealpha=0.85,
                        facecolor=PANEL_BG, edgecolor=BORDER_CLR,
                        labelcolor=TEXT_CLR)
        if leg:
            leg.get_frame().set_facecolor(PANEL_BG)

        self.fig_delta.tight_layout()
        self._delta_canvas.draw_idle()

    # ══════════════════════════════════════════════════════════
    #  TAB 3 — PLC READER
    # ══════════════════════════════════════════════════════════
    def _build_plc_tab(self, parent):
        if not SNAP7_AVAILABLE:
            wrap = ttk.Frame(parent); wrap.pack(fill="both", expand=True, padx=20, pady=20)
            tk.Label(wrap, text="⚠ python-snap7 non installato",
                     bg=DARK_BG, fg=WARN_CLR,
                     font=("Consolas", 14, "bold")).pack(pady=10)
            tk.Label(wrap, text="Installa con:   pip install python-snap7",
                     bg=DARK_BG, fg=TEXT_CLR,
                     font=("Consolas", 11)).pack(pady=4)
            tk.Label(wrap, text="Poi riavvia l'applicazione.",
                     bg=DARK_BG, fg=MUTED_CLR,
                     font=("Consolas", 10)).pack()
            return

        top = ttk.LabelFrame(parent, text="  Connessione S7-1500/1200  ", padding=8)
        top.pack(fill="x", padx=8, pady=6)

        self._pv_plc_ip   = tk.StringVar(value=self._settings.get('plc_ip', '192.168.0.1'))
        self._pv_plc_rack = tk.StringVar(value=self._settings.get('plc_rack', '0'))
        self._pv_plc_slot = tk.StringVar(value=self._settings.get('plc_slot', '1'))
        self._pv_plc_db   = tk.StringVar(value=self._settings.get('plc_db', '100'))

        r1 = ttk.Frame(top); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="IP:", width=6).pack(side="left")
        ttk.Entry(r1, textvariable=self._pv_plc_ip, width=16,
                  font=("Consolas", 10)).pack(side="left", padx=2)
        ttk.Label(r1, text="Rack:", width=6).pack(side="left", padx=(10, 0))
        ttk.Entry(r1, textvariable=self._pv_plc_rack, width=4).pack(side="left")
        ttk.Label(r1, text="Slot:", width=6).pack(side="left", padx=(10, 0))
        ttk.Entry(r1, textvariable=self._pv_plc_slot, width=4).pack(side="left")
        ttk.Label(r1, text="DB #:", width=6).pack(side="left", padx=(10, 0))
        ttk.Entry(r1, textvariable=self._pv_plc_db, width=6).pack(side="left")

        r2 = ttk.Frame(top); r2.pack(fill="x", pady=4)
        self._btn_plc_connect = ttk.Button(r2, text="🔗 Connetti",
            style="Plc.TButton", command=self._plc_connect)
        self._btn_plc_connect.pack(side="left", padx=2)
        self._btn_plc_disconnect = ttk.Button(r2, text="❌ Disconnetti",
            command=self._plc_disconnect, state="disabled")
        self._btn_plc_disconnect.pack(side="left", padx=2)
        self._btn_plc_read = ttk.Button(r2, text="📥 Leggi DB",
            style="Plc.TButton", command=self._plc_read_now, state="disabled")
        self._btn_plc_read.pack(side="left", padx=8)
        self._btn_plc_save_viewer = ttk.Button(r2, text="📊 Carica nel Viewer",
            command=self._plc_load_viewer, state="disabled")
        self._btn_plc_save_viewer.pack(side="left", padx=2)

        self._pv_plc_status = tk.StringVar(value="● Disconnesso")
        tk.Label(r2, textvariable=self._pv_plc_status,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9, "bold")).pack(side="right", padx=4)

        # Log PLC
        log_lf = ttk.LabelFrame(parent, text="  Log PLC  ", padding=4)
        log_lf.pack(fill="both", expand=True, padx=8, pady=6)
        sb = ttk.Scrollbar(log_lf); sb.pack(side="right", fill="y")
        self._plc_log = tk.Text(log_lf, bg=DARK_BG, fg=TEXT_CLR,
            font=("Consolas", 9), wrap="word", yscrollcommand=sb.set,
            insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self._plc_log.pack(fill="both", expand=True)
        sb.config(command=self._plc_log.yview)
        for tag, color in [("ok", OK_CLR), ("err", ERR_CLR),
                            ("info", ACCENT), ("warn", WARN_CLR)]:
            self._plc_log.tag_config(tag, foreground=color)

        self._plc_log_msg("=== PLC Reader Log ===\n", "info")
        self._plc_log_msg("Configura IP/Rack/Slot/DB e premi Connetti.\n")

        self._plc_client = None
        self._plc_last_decoded = None

    def _plc_log_msg(self, msg, tag=""):
        if hasattr(self, '_plc_log'):
            self._plc_log.insert("end", msg, tag)
            self._plc_log.see("end")

    def _plc_connect(self):
        try:
            ip = self._pv_plc_ip.get().strip()
            rack = int(self._pv_plc_rack.get() or 0)
            slot = int(self._pv_plc_slot.get() or 1)
            self._plc_log_msg(f"\n→ Connessione a {ip} (rack={rack}, slot={slot})...\n", "info")
            self._plc_client = PLCReader(ip, rack, slot)
            cpu, pdu = self._plc_client.connect()
            self._plc_log_msg(f"✓ Connesso — CPU: {cpu}, PDU: {pdu} byte\n", "ok")
            self._pv_plc_status.set("● Connesso")
            self._btn_plc_connect.config(state="disabled")
            self._btn_plc_disconnect.config(state="normal")
            self._btn_plc_read.config(state="normal")
        except Exception as e:
            self._plc_log_msg(f"✗ Errore connessione: {e}\n", "err")
            self._pv_plc_status.set("● Errore")
            messagebox.showerror("Errore PLC", str(e))

    def _plc_disconnect(self):
        try:
            if self._plc_client:
                self._plc_client.disconnect()
            self._plc_log_msg("✓ Disconnesso.\n", "ok")
        except Exception as e:
            self._plc_log_msg(f"Errore disconnect: {e}\n", "warn")
        self._plc_client = None
        self._pv_plc_status.set("● Disconnesso")
        self._btn_plc_connect.config(state="normal")
        self._btn_plc_disconnect.config(state="disabled")
        self._btn_plc_read.config(state="disabled")
        self._btn_plc_save_viewer.config(state="disabled")

    def _plc_read_now(self):
        if not self._plc_client:
            return
        try:
            db_num = int(self._pv_plc_db.get())
            offset_map, total_size = plc_build_offset_map()
            self._plc_log_msg(f"→ Leggo DB{db_num} ({total_size} byte)...\n", "info")
            t0 = time.time()
            raw = self._plc_client.read_db_raw(db_num, total_size)
            dt = (time.time() - t0) * 1000
            self._plc_log_msg(f"✓ {total_size} byte letti in {dt:.0f} ms\n", "ok")

            decoded = plc_decode_db(raw, offset_map)
            self._plc_last_decoded = decoded
            sc = decoded['scalars']
            self._plc_log_msg(
                f"  Sp.medio={sc.get('AppSpessoreMedio',0):.3f} mm  "
                f"Sp.max={sc.get('AppSpessoreMax',0):.3f} mm  "
                f"ΔMax={sc.get('AppDeltaMax',0):.3f} mm  "
                f"n={int(sc.get('AppNcelleProfilo',0))}\n", "ok")
            if sc.get('AppSpessoreNok'):
                self._plc_log_msg("  ⚠ DOPPIO SPESSORE RILEVATO\n", "err")
            self._btn_plc_save_viewer.config(state="normal")
        except Exception as e:
            self._plc_log_msg(f"✗ Errore lettura: {e}\n", "err")

    def _plc_load_viewer(self):
        if not self._plc_last_decoded:
            return
        db_name = f"DB{self._pv_plc_db.get()}_{datetime.datetime.now().strftime('%H%M%S')}"
        text = plc_generate_db_text(self._plc_last_decoded, db_name=db_name)
        data = parse_db_file_from_text(text, filename=db_name + ".db")
        self._load_data(data)
        self._plc_log_msg("✓ Dati caricati nel Viewer.\n", "ok")
        self._nb.select(0)

    # ══════════════════════════════════════════════════════════
    #  TAB 4 — AUTO-EXPORT
    # ══════════════════════════════════════════════════════════
    def _build_autoexp_tab(self, parent):
        if not SNAP7_AVAILABLE:
            wrap = ttk.Frame(parent); wrap.pack(fill="both", expand=True, padx=20, pady=20)
            tk.Label(wrap, text="⚠ python-snap7 richiesto per Auto-Export",
                     bg=DARK_BG, fg=WARN_CLR,
                     font=("Consolas", 12, "bold")).pack(pady=10)
            return

        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, width=360); pane.add(left, weight=0)
        left.pack_propagate(False)
        right = ttk.Frame(pane); pane.add(right, weight=1)

        # Settings
        lf1 = ttk.LabelFrame(left, text="  Configurazione  ", padding=6)
        lf1.pack(fill="x", padx=6, pady=4)

        r1 = ttk.Frame(lf1); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Poll:", width=8).pack(side="left")
        self._pv_autoexp_poll = tk.StringVar(value="100")
        ttk.Entry(r1, textvariable=self._pv_autoexp_poll, width=8
                  ).pack(side="left", padx=2)
        ttk.Label(r1, text="ms", style="Muted.TLabel").pack(side="left")

        r2 = ttk.Frame(lf1); r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="Trigger:", width=8).pack(side="left")
        self._pv_autoexp_trig = tk.StringVar(value="Variazione AppSpessoreMedio")
        cmb = ttk.Combobox(r2, textvariable=self._pv_autoexp_trig,
            width=26, state="readonly",
            values=["Variazione AppSpessoreMedio",
                    "O_TaraturaCompletata ↑",
                    "Falling edge Pinza.InZonaControllo"])
        cmb.current(0); cmb.pack(side="left", padx=2)

        # Path OK
        lf2 = ttk.LabelFrame(left, text="  Cartella OK  ", padding=6)
        lf2.pack(fill="x", padx=6, pady=4)
        self._pv_autoexp_path = tk.StringVar(
            value=self._settings.get('autoexp_path_ok',
                os.path.join(os.path.expanduser('~'), 'ThicknessExport', 'ok')))
        r3 = ttk.Frame(lf2); r3.pack(fill="x")
        ttk.Entry(r3, textvariable=self._pv_autoexp_path,
                  font=("Consolas", 9)).pack(side="left", fill="x", expand=True)
        ttk.Button(r3, text="📁", width=3,
                   command=lambda: self._browse_dir(self._pv_autoexp_path)
                   ).pack(side="left", padx=2)

        # Path NOK
        lf3 = ttk.LabelFrame(left, text="  Cartella NOK / Scarto  ", padding=6)
        lf3.pack(fill="x", padx=6, pady=4)
        self._pv_autoexp_path_rej = tk.StringVar(
            value=self._settings.get('autoexp_path_nok',
                os.path.join(os.path.expanduser('~'), 'ThicknessExport', 'nok')))
        r4 = ttk.Frame(lf3); r4.pack(fill="x")
        ttk.Entry(r4, textvariable=self._pv_autoexp_path_rej,
                  font=("Consolas", 9)).pack(side="left", fill="x", expand=True)
        ttk.Button(r4, text="📁", width=3,
                   command=lambda: self._browse_dir(self._pv_autoexp_path_rej)
                   ).pack(side="left", padx=2)

        # Opzioni
        lf4 = ttk.LabelFrame(left, text="  Opzioni salvataggio  ", padding=6)
        lf4.pack(fill="x", padx=6, pady=4)
        self._pv_autoexp_save_file = tk.BooleanVar(value=True)
        self._pv_autoexp_save_sql = tk.BooleanVar(value=True)
        self._pv_autoexp_load_viewer = tk.BooleanVar(value=True)
        for var, text in [(self._pv_autoexp_save_file, "Salva file .db"),
                          (self._pv_autoexp_save_sql,  "Salva in SQLite"),
                          (self._pv_autoexp_load_viewer,"Carica nel Viewer")]:
            tk.Checkbutton(lf4, text=text, variable=var,
                bg=DARK_BG, fg=TEXT_CLR, selectcolor="#1f6feb",
                activebackground=DARK_BG, font=("Consolas", 9),
                anchor="w").pack(fill="x", pady=1)

        # Path SQLite
        lf5 = ttk.LabelFrame(left, text="  Archivio SQLite  ", padding=6)
        lf5.pack(fill="x", padx=6, pady=4)
        self._pv_sql_path = tk.StringVar(
            value=self._settings.get('sqlite_path',
                os.path.join(os.path.expanduser('~'), 'ThicknessExport', 'archive.sqlite')))
        r5 = ttk.Frame(lf5); r5.pack(fill="x")
        ttk.Entry(r5, textvariable=self._pv_sql_path,
                  font=("Consolas", 9)).pack(side="left", fill="x", expand=True)
        ttk.Button(r5, text="📁", width=3,
                   command=lambda: self._browse_file_save(self._pv_sql_path,
                       [("SQLite", "*.sqlite"), ("Tutti", "*.*")])
                   ).pack(side="left", padx=2)

        # Contatori + pulsanti
        lf6 = ttk.LabelFrame(left, text="  Monitoraggio  ", padding=6)
        lf6.pack(fill="x", padx=6, pady=4)
        r6 = ttk.Frame(lf6); r6.pack(fill="x")
        self._btn_autoexp_start = tk.Button(r6, text="▶ Avvia",
            bg=OK_CLR, fg=DARK_BG, font=("Consolas", 10, "bold"),
            command=self._autoexp_start, width=10)
        self._btn_autoexp_start.pack(side="left", padx=2)
        self._btn_autoexp_stop = tk.Button(r6, text="■ Stop",
            bg=ERR_CLR, fg=DARK_BG, font=("Consolas", 10, "bold"),
            command=self._autoexp_stop, state="disabled", width=10)
        self._btn_autoexp_stop.pack(side="left", padx=2)

        self._pv_autoexp_status = tk.StringVar(value="● Fermo")
        tk.Label(lf6, textvariable=self._pv_autoexp_status,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 10, "bold")).pack(anchor="w", pady=4)

        self._pv_autoexp_count = tk.StringVar(value="✓ 0  ✗ 0  ⚙ 0")
        tk.Label(lf6, textvariable=self._pv_autoexp_count,
                 bg=DARK_BG, fg=AUTOEXP_CLR,
                 font=("Consolas", 11, "bold")).pack(anchor="w")
        self._pv_autoexp_last = tk.StringVar(value="")
        tk.Label(lf6, textvariable=self._pv_autoexp_last,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 8)).pack(anchor="w")

        # Log
        log_lf = ttk.LabelFrame(right, text="  Log Auto-Export  ", padding=4)
        log_lf.pack(fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(log_lf); sb.pack(side="right", fill="y")
        self._ae_log = tk.Text(log_lf, bg=DARK_BG, fg=TEXT_CLR,
            font=("Consolas", 9), wrap="word", yscrollcommand=sb.set,
            insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self._ae_log.pack(fill="both", expand=True)
        sb.config(command=self._ae_log.yview)
        for tag, color in [("ok", OK_CLR), ("err", ERR_CLR),
                            ("info", ACCENT), ("warn", WARN_CLR),
                            ("tar", "#d2a8ff")]:
            self._ae_log.tag_config(tag, foreground=color)
        self._ae_log.insert("end", "=== Auto-Export Log ===\n", "info")
        self._ae_log.insert("end",
            "Configura cartelle e premi Avvia per iniziare il monitoraggio.\n\n")

    def _ae_log_msg(self, msg, tag=""):
        if hasattr(self, '_ae_log'):
            self._ae_log.insert("end", msg, tag)
            self._ae_log.see("end")

    def _autoexp_start(self):
        # Verifica PLC connesso
        if not self._plc_client:
            messagebox.showwarning("PLC non connesso",
                "Connettiti al PLC prima di avviare l'Auto-Export.")
            self._nb.select(2)  # Tab PLC
            return

        # Crea cartelle
        try:
            if self._pv_autoexp_save_file.get():
                os.makedirs(self._pv_autoexp_path.get(), exist_ok=True)
                os.makedirs(self._pv_autoexp_path_rej.get(), exist_ok=True)
        except Exception as e:
            messagebox.showerror("Errore", f"Impossibile creare cartelle: {e}")
            return

        # Apre SQLite
        if self._pv_autoexp_save_sql.get():
            try:
                self._autoexp_sql_con = sqlite_init(self._pv_sql_path.get())
            except Exception as e:
                messagebox.showerror("Errore SQLite", str(e))
                return

        self._autoexp_client = self._plc_client
        self._autoexp_running = True
        self._autoexp_prev_sentinel = None
        self._autoexp_count_ok = 0
        self._autoexp_count_nok = 0
        self._autoexp_count_tar = 0
        self._pv_autoexp_count.set("✓ 0  ✗ 0  ⚙ 0")
        self._pv_autoexp_status.set("● Monitoraggio attivo")

        self._btn_autoexp_start.config(state="disabled")
        self._btn_autoexp_stop.config(state="normal")

        self._ae_log_msg(
            f"\n▶ Avvio monitoraggio — poll {self._pv_autoexp_poll.get()}ms — "
            f"trigger: {self._pv_autoexp_trig.get()}\n", "info")

        self._autoexp_poll()

    def _autoexp_stop(self):
        self._autoexp_running = False
        if self._autoexp_timer_id:
            try: self.after_cancel(self._autoexp_timer_id)
            except Exception: pass
            self._autoexp_timer_id = None
        if self._autoexp_sql_con:
            try: self._autoexp_sql_con.close()
            except Exception: pass
            self._autoexp_sql_con = None

        self._btn_autoexp_start.config(state="normal")
        self._btn_autoexp_stop.config(state="disabled")
        self._pv_autoexp_status.set("● Fermo")
        self._ae_log_msg("■ Monitoraggio fermato.\n\n", "warn")

    def _autoexp_poll(self):
        """Polling ciclico: legge sentinella e, su trigger, esporta."""
        if not self._autoexp_running:
            return

        try:
            # Leggo TUTTO il DB ogni poll. Per ottimizzare si può leggere
            # solo la sentinella e poi il resto su trigger (migliorabile v1.1).
            db_num = int(self._pv_plc_db.get())
            offset_map, total_size = plc_build_offset_map()
            raw = self._autoexp_client.read_db_raw(db_num, total_size, chunk=900)
            decoded = plc_decode_db(raw, offset_map)
            sc = decoded['scalars']

            # Calcolo sentinella per rilevamento nuovo dato
            trig = self._pv_autoexp_trig.get()
            if trig.startswith("Variazione"):
                sentinel = round(float(sc.get('AppSpessoreMedio', 0.0)), 6)
            elif trig.startswith("O_TaraturaCompletata"):
                sentinel = bool(sc.get('O_TaraturaCompletata', False))
            else:   # Falling edge InZonaControllo
                sentinel = bool(sc.get('Pinza.InZonaControllo', False))

            triggered = False
            if self._autoexp_prev_sentinel is None:
                self._autoexp_prev_sentinel = sentinel
            else:
                if trig.startswith("Falling edge"):
                    # falling = era TRUE, ora è FALSE
                    triggered = (self._autoexp_prev_sentinel and not sentinel)
                elif trig.startswith("O_TaraturaCompletata"):
                    # rising = era FALSE, ora è TRUE
                    triggered = (not self._autoexp_prev_sentinel and sentinel)
                else:
                    # variazione = valore diverso
                    triggered = (sentinel != self._autoexp_prev_sentinel)
                self._autoexp_prev_sentinel = sentinel

            if triggered:
                self._autoexp_on_trigger(decoded, db_num)

        except Exception as e:
            self._ae_log_msg(f"✗ Errore polling: {e}\n", "err")
            self._pv_autoexp_status.set("● Errore polling")

        # Ri-schedula
        if self._autoexp_running:
            try:
                ms = max(20, int(self._pv_autoexp_poll.get()))
            except ValueError:
                ms = 100
            self._autoexp_timer_id = self.after(ms, self._autoexp_poll)

    def _autoexp_on_trigger(self, decoded, db_num):
        """Evento trigger: salva file + SQLite + opz. carica in viewer."""
        sc = decoded['scalars']
        ts = datetime.datetime.now()
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        ts_file = ts.strftime("%Y%m%d_%H%M%S_%f")[:18]

        is_taratura = bool(sc.get('O_TaraturaAttiva', False)) or \
                      (self._pv_autoexp_trig.get().startswith("O_TaraturaCompletata"))
        is_nok = bool(sc.get('AppSpessoreNok', sc.get('O_SpessoreNOK', False)))

        # Categoria
        if is_taratura:
            prefix = "TARAT"
            dest_dir = self._pv_autoexp_path.get()   # tarature in cartella OK
            self._autoexp_count_tar += 1
            tag = "tar"
        elif is_nok:
            prefix = "NOK"
            dest_dir = self._pv_autoexp_path_rej.get()
            self._autoexp_count_nok += 1
            tag = "err"
        else:
            prefix = "OK"
            dest_dir = self._pv_autoexp_path.get()
            self._autoexp_count_ok += 1
            tag = "ok"

        seq = self._autoexp_count_ok + self._autoexp_count_nok + self._autoexp_count_tar
        filename = f"Spess_DB{db_num}_{prefix}_{ts_file}_{seq:04d}.db"
        db_text = plc_generate_db_text(decoded,
            db_name=f"Spess_DB{db_num}_{ts_file}")

        # Salva file
        if self._pv_autoexp_save_file.get():
            try:
                os.makedirs(dest_dir, exist_ok=True)
                filepath = os.path.join(dest_dir, filename)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(db_text)
            except Exception as e:
                self._ae_log_msg(f"  ✗ err file: {e}\n", "err")

        # Salva SQLite
        if self._pv_autoexp_save_sql.get() and self._autoexp_sql_con:
            try:
                rid = sqlite_insert(self._autoexp_sql_con, decoded,
                                     filename, db_num, is_taratura)
                self._ae_log_msg(f"  │ SQLite #{rid}  ", "")
            except Exception as e:
                self._ae_log_msg(f"  ✗ err sqlite: {e}\n", "err")

        # Log
        icon = "⚙" if is_taratura else ("✗" if is_nok else "✓")
        sp_med = sc.get('AppSpessoreMedio', sc.get('O_SpessoreMedio', 0))
        d_max  = sc.get('AppDeltaMax', sc.get('O_DeltaMax', 0))
        n_cel  = int(sc.get('AppNcelleProfilo', sc.get('O_nCelleProfilo', 0)))

        self._ae_log_msg(
            f"{icon} {prefix} #{seq} [{ts.strftime('%H:%M:%S')}]  "
            f"Sp.med={sp_med:.3f}  ΔMax={d_max:.3f}  n={n_cel}\n", tag)

        # Aggiorna contatori UI
        self._pv_autoexp_count.set(
            f"✓ {self._autoexp_count_ok}  "
            f"✗ {self._autoexp_count_nok}  "
            f"⚙ {self._autoexp_count_tar}")
        self._pv_autoexp_last.set(f"{icon} {ts.strftime('%H:%M:%S')}  {filename[:40]}")

        # Carica nel viewer
        if self._pv_autoexp_load_viewer.get():
            try:
                data = parse_db_file_from_text(db_text, filename=filename)
                self._load_data(data)
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════
    #  TAB 5 — HISTORY
    # ══════════════════════════════════════════════════════════
    def _build_history_tab(self, parent):
        bar = ttk.Frame(parent); bar.pack(fill="x", padx=6, pady=6)
        ttk.Label(bar, text="Cartella:").pack(side="left")
        self._pv_hist_path = tk.StringVar(
            value=self._settings.get('autoexp_path_ok',
                os.path.join(os.path.expanduser('~'), 'ThicknessExport', 'ok')))
        ttk.Entry(bar, textvariable=self._pv_hist_path, width=50,
                  font=("Consolas", 9)).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(bar, text="📁",
                   command=lambda: self._browse_dir(self._pv_hist_path)
                   ).pack(side="left", padx=2)
        ttk.Button(bar, text="🔄 Aggiorna",
                   command=self._history_refresh).pack(side="left", padx=2)

        # Lista
        lf = ttk.LabelFrame(parent, text="  Passate archiviate  ", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=6)

        cols = ("time", "file", "verdict", "medio", "max", "delta_max", "n")
        tree = ttk.Treeview(lf, columns=cols, show="headings", height=20)
        for c, txt, w, anc in [
                ("time", "Ora", 120, "w"),
                ("file", "File", 340, "w"),
                ("verdict", "Esito", 80, "center"),
                ("medio", "Sp. medio", 80, "e"),
                ("max", "Sp. max", 80, "e"),
                ("delta_max", "Δ max", 80, "e"),
                ("n", "N", 50, "e")]:
            tree.heading(c, text=txt)
            tree.column(c, width=w, anchor=anc)
        tree.tag_configure('ok', foreground=OK_CLR)
        tree.tag_configure('nok', foreground=ERR_CLR)
        tree.tag_configure('tar', foreground="#d2a8ff")

        sb = ttk.Scrollbar(lf, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda e: self._history_open_selected())
        self._hist_tree = tree

        bar2 = ttk.Frame(parent); bar2.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar2, text="📊 Apri nel Viewer",
                   style="Accent.TButton",
                   command=self._history_open_selected).pack(side="left", padx=2)
        ttk.Button(bar2, text="🗑 Elimina selezionato",
                   command=self._history_delete_selected).pack(side="left", padx=2)
        self._pv_hist_count = tk.StringVar(value="0 file")
        tk.Label(bar2, textvariable=self._pv_hist_count,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9)).pack(side="right", padx=4)

    def _history_refresh(self):
        path = self._pv_hist_path.get()
        if not os.path.isdir(path):
            messagebox.showwarning("Cartella non trovata", path)
            return
        self._hist_tree.delete(*self._hist_tree.get_children())
        files = sorted(glob.glob(os.path.join(path, "*.db")),
                       key=os.path.getmtime, reverse=True)
        n = 0
        for fp in files[:500]:   # max 500
            try:
                stat = os.stat(fp)
                ts = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                fn = os.path.basename(fp)
                # Estrazione rapida esito dal nome
                if "_TARAT_" in fn:   tag = 'tar';  verdict = "TARAT"
                elif "_NOK_" in fn:   tag = 'nok';  verdict = "NOK"
                elif "_OK_" in fn:    tag = 'ok';   verdict = "OK"
                else:                  tag = '';     verdict = "—"

                # Per evitare parse di ogni file, mostra solo nome+timestamp;
                # valori riempiti con — (si popolano all'apertura)
                self._hist_tree.insert("", "end",
                    values=(ts, fn, verdict, "—", "—", "—", "—"),
                    tags=(tag,), iid=fp)
                n += 1
            except Exception:
                pass
        self._pv_hist_count.set(f"{n} file")

    def _history_open_selected(self):
        sel = self._hist_tree.selection()
        if not sel: return
        fp = sel[0]
        try:
            data = parse_db_file(fp)
            self._load_data(data)
            self._nb.select(0)
        except Exception as e:
            messagebox.showerror("Errore", f"Impossibile aprire:\n{e}")

    def _history_delete_selected(self):
        sel = self._hist_tree.selection()
        if not sel: return
        if not messagebox.askyesno("Conferma",
                f"Eliminare {len(sel)} file?"):
            return
        for fp in sel:
            try: os.remove(fp)
            except Exception as e:
                self.app_log(f"Errore eliminazione {fp}: {e}", "warn")
        self._history_refresh()

    # ══════════════════════════════════════════════════════════
    #  TAB 6 — IMPOSTAZIONI
    # ══════════════════════════════════════════════════════════
    def _build_settings_tab(self, parent):
        wrap = ttk.Frame(parent); wrap.pack(fill="both", expand=True, padx=12, pady=12)

        ttk.Label(wrap, text="Impostazioni applicazione",
                  style="Title.TLabel").pack(anchor="w", pady=(0, 8))

        info_lf = ttk.LabelFrame(wrap, text="  Info  ", padding=8)
        info_lf.pack(fill="x", pady=4)
        tk.Label(info_lf, text=f"Versione: {APP_RELEASE}",
                 bg=DARK_BG, fg=ACCENT, font=("Consolas", 10)
                 ).pack(anchor="w", pady=1)
        tk.Label(info_lf,
                 text=f"Snap7: {'✓ disponibile' if SNAP7_AVAILABLE else '✗ non installato'}",
                 bg=DARK_BG,
                 fg=OK_CLR if SNAP7_AVAILABLE else WARN_CLR,
                 font=("Consolas", 10)).pack(anchor="w", pady=1)
        tk.Label(info_lf, text=f"Settings file: {self._settings_file}",
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas", 9)
                 ).pack(anchor="w", pady=1)

        btn_lf = ttk.Frame(wrap); btn_lf.pack(fill="x", pady=12)
        ttk.Button(btn_lf, text="💾 Salva impostazioni",
                   style="Accent.TButton",
                   command=self._save_settings).pack(side="left", padx=2)
        ttk.Button(btn_lf, text="📂 Apri cartella settings",
                   command=lambda: self._open_path(
                       os.path.dirname(self._settings_file))
                   ).pack(side="left", padx=2)

        # Note
        notes_lf = ttk.LabelFrame(wrap, text="  Note tecniche  ", padding=8)
        notes_lf.pack(fill="both", expand=True, pady=4)
        notes = tk.Text(notes_lf, bg=PANEL_BG, fg=TEXT_CLR,
            font=("Consolas", 9), wrap="word", height=12,
            insertbackground=TEXT_CLR)
        notes.pack(fill="both", expand=True)
        notes.insert("end", """
LETTURA DIRETTA PLC (Snap7)
 • Il FB Fb935_ControlloSpessoreAlVolo usa S7_Optimized_Access := 'TRUE'.
 • Per leggere via Snap7 sono necessarie due opzioni:
   A) Impostare S7_Optimized_Access := 'FALSE' sul DB istanza.
      (attenzione: peggiora le prestazioni con array 1000 celle)
   B) Creare un DB "shadow" non-ottimizzato che riceve i dati dal
      FB tramite MOVE o blocco STRUCTURE_COPY. Leggere quel DB.
 • Abilitare "Consenti PUT/GET da partner remoto" nelle proprietà CPU.
 • Nessun password utente impostata sul DB.

TRIGGER AUTO-EXPORT
 • "Variazione AppSpessoreMedio": salva quando il valore cambia
   (lieve rischio di falso trigger su rumore numerico).
 • "O_TaraturaCompletata ↑": salva solo al completamento di una taratura.
 • "Falling edge Pinza.InZonaControllo": salva a fine passata
   (più preciso, richiede che la variabile sia decodificata correttamente).

ESTENSIONI FUTURE
 • Monitor real-time (trace scroll) della posizione asse e lettura laser
 • Query SQLite: trend temporali, istogrammi deviazioni
 • Export XLSX con statistiche aggregate
 • Gestione multi-DB (linee con più pinze)
""")
        notes.config(state="disabled")

    # ══════════════════════════════════════════════════════════
    #  HELPERS
    # ══════════════════════════════════════════════════════════
    def app_log(self, msg, tag="info"):
        self._lbl_status.config(text=msg)

    def _open_file(self):
        fp = filedialog.askopenfilename(
            title="Apri file .db TIA Portal",
            filetypes=[("TIA Portal DB", "*.db"), ("Tutti i file", "*.*")])
        if not fp: return
        try:
            data = parse_db_file(fp)
            self._load_data(data)
        except Exception as e:
            messagebox.showerror("Errore parser", str(e))

    def _save_as_db(self):
        if not self.db_data:
            messagebox.showinfo("Info", "Nessun dato caricato.")
            return
        fp = filedialog.asksaveasfilename(
            title="Salva come file .db",
            defaultextension=".db",
            filetypes=[("TIA Portal DB", "*.db")])
        if not fp: return
        try:
            text = self.db_data.get('raw_text', '')
            if not text:
                # Rigenera da scalars/arrays
                decoded = {'scalars': self.db_data.get('scalars', {}),
                           'arrays':  self.db_data.get('arrays', {})}
                text = plc_generate_db_text(decoded, db_name="Export")
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(text)
            self.app_log(f"Salvato: {fp}")
        except Exception as e:
            messagebox.showerror("Errore salvataggio", str(e))

    def _save_plot(self, fig):
        fp = filedialog.asksaveasfilename(
            title="Salva grafico come PNG",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("SVG", "*.svg"), ("PDF", "*.pdf")])
        if not fp: return
        try:
            fig.savefig(fp, dpi=150, facecolor=DARK_BG, bbox_inches='tight')
            self.app_log(f"Grafico salvato: {fp}")
        except Exception as e:
            messagebox.showerror("Errore", str(e))

    def _browse_dir(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or os.path.expanduser('~'))
        if d: var.set(d)

    def _browse_file_save(self, var, filetypes):
        fp = filedialog.asksaveasfilename(
            initialfile=os.path.basename(var.get()),
            initialdir=os.path.dirname(var.get()) or os.path.expanduser('~'),
            filetypes=filetypes)
        if fp: var.set(fp)

    def _open_path(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess; subprocess.Popen(["open", path])
            else:
                import subprocess; subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Errore", str(e))

    # ─── Caricamento dati in UI ───────────────────────────────
    def _load_data(self, data):
        self.db_data = data
        fn = data.get('filename', '—')
        self._lbl_file.config(text=fn)
        self._update_results_panel()
        self._draw_viewer()
        self._draw_delta()
        self.app_log(f"Caricato: {fn}")

    def _update_results_panel(self):
        if not self.db_data: return
        sc = self.db_data.get('scalars', {})

        # Verdict
        nok = bool(sc.get('AppSpessoreNok', sc.get('O_SpessoreNOK', False)))
        disab = bool(sc.get('I_ParametriCntrolloSpessore.DisabilitaControllo',
                    sc.get('DisabilitaControllo', False)))
        if disab:
            self._pv_verdict.set("⚠ DISABILITATO")
        elif nok:
            self._pv_verdict.set("✗ DOPPIO SPESSORE")
        else:
            self._pv_verdict.set("✓ OK")
        # Colore verdict
        widgets_to_color = [w for w in self._pv_verdict.__dict__.values()]
        # Tk StringVar non ha widget direttamente — aggiorniamo label nel box_res.
        # La label padre eredita il colore, semplifichiamo con after
        self.after(10, self._recolor_verdict)

        # Scalari
        self._pv_spess_med.set(f"{sc.get('AppSpessoreMedio', sc.get('O_SpessoreMedio', 0)):.3f} mm")
        self._pv_spess_max.set(f"{sc.get('AppSpessoreMax', sc.get('O_SpessoreMax', 0)):.3f} mm")
        self._pv_delta_med.set(f"{sc.get('AppDeltaMedio', sc.get('O_DeltaMedio', 0)):.3f} mm")
        self._pv_delta_max.set(f"{sc.get('AppDeltaMax', sc.get('O_DeltaMax', 0)):.3f} mm")
        self._pv_n_celle.set(f"{int(sc.get('AppNcelleProfilo', sc.get('O_nCelleProfilo', 0)))} / {ARRAY_SIZE}")

        # Taratura
        tar_att = bool(sc.get('O_TaraturaAttiva', sc.get('taraturaInCorso', False)))
        tar_ok  = bool(sc.get('O_TaraturaCompletata', sc.get('baselineValida', False)))
        if tar_att:
            self._pv_tar_stato.set("⚙ Taratura in corso...")
        elif tar_ok:
            self._pv_tar_stato.set("✓ Baseline valida")
        else:
            self._pv_tar_stato.set("⚠ Baseline NON tarata")

        sp_rif = sc.get('I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento',
                        sc.get('SpessoreDiscoRiferimento', 0.0))
        self._pv_tar_rif.set(f"Riferimento: {sp_rif:.3f} mm")

        # Parametri UDT
        for key, var in self._pv_params.items():
            full_key = f"I_ParametriCntrolloSpessore.{key}"
            val = sc.get(full_key, sc.get(key, None))
            if val is None:
                var.set("—")
            elif isinstance(val, bool):
                var.set("SÌ" if val else "NO")
            elif isinstance(val, (int, float)):
                var.set(f"{val:.3f}" if isinstance(val, float) else str(val))
            else:
                var.set(str(val))

        # Pinza
        self._pv_piece.set("SÌ" if sc.get('I_PiecePresence', False) else "NO")
        self._pv_zona_ctrl.set("SÌ" if sc.get('Pinza.InZonaControllo', False) else "NO")
        self._pv_zona_rall.set("SÌ" if sc.get('Pinza.InZonaRallenta', False) else "NO")

    def _recolor_verdict(self):
        # Colore in base al contenuto
        txt = self._pv_verdict.get()
        for child in self._left_panel.winfo_children():
            self._recurse_recolor(child, txt)

    def _recurse_recolor(self, widget, verdict_txt):
        try:
            # Cerca la label che mostra verdict (font 18)
            if isinstance(widget, tk.Label):
                font = widget.cget("font")
                if "18" in str(font):
                    if "OK" in verdict_txt:
                        widget.config(fg=OK_CLR)
                    elif "DOPPIO" in verdict_txt:
                        widget.config(fg=ERR_CLR)
                    elif "DISAB" in verdict_txt:
                        widget.config(fg=WARN_CLR)
                    else:
                        widget.config(fg=MUTED_CLR)
        except Exception:
            pass
        for ch in widget.winfo_children():
            self._recurse_recolor(ch, verdict_txt)


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = ThicknessApp()
    app.mainloop()
