# -*- coding: utf-8 -*-
"""
ThicknessProfiler DB Analyzer  v2.0.0
Analizzatore per Fb936_ControlloSpessore_v0 (TIA Portal S7-1500).

Caratteristiche:
  - Viewer profilo spessore + baseline + delta (1000 celle)
  - PLC Reader via Snap7 (DB istanza NON-ottimizzato)
  - Auto-Export: polling + archiviazione su SQLite
  - History: query su archivio SQLite con riapertura passate
  - Setup persistito in thickness_viewer.ini nella stessa cartella del .pyw

Requisiti: pip install matplotlib numpy
Opzionale: pip install python-snap7  (per tab PLC Reader / Auto-Export)
Build EXE: pyinstaller --onefile --windowed thickness_viewer.pyw
"""

APP_VERSION = "2.0.0"
APP_BUILD   = "2026-04-21"
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

# ── Pulizia processi alla chiusura ──────────────────────
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
import configparser

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import numpy as np

# ── SNAP7 (opzionale) ────────────
SNAP7_AVAILABLE = False
try:
    import snap7
    SNAP7_AVAILABLE = True
except ImportError:
    pass


# ══════════════════════════════════════════════════════════════════
#  APP DIR / SETTINGS
# ══════════════════════════════════════════════════════════════════

def get_app_dir():
    """Cartella dove sono .pyw o .exe — dove va letto/scritto il file .ini."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_FILENAME = "thickness_viewer.ini"


def load_settings():
    """Legge thickness_viewer.ini dalla cartella dell'applicazione.
    Crea default se assente."""
    path = os.path.join(get_app_dir(), SETTINGS_FILENAME)
    cfg = configparser.ConfigParser()
    # Defaults
    cfg['PLC'] = {
        'ip':   '192.168.0.1',
        'rack': '0',
        'slot': '1',
        'db':   '16010',
    }
    cfg['SQL'] = {
        'path': 'thickness_archive.sqlite',
    }
    if os.path.isfile(path):
        try:
            cfg.read(path, encoding='utf-8')
        except Exception:
            pass
    return cfg, path


def save_settings(cfg, path):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            cfg.write(f)
        return True
    except Exception:
        return False


def resolve_sql_path(raw):
    """Se relativo, risolto rispetto alla cartella dell'app."""
    if os.path.isabs(raw):
        return raw
    return os.path.join(get_app_dir(), raw)


# ══════════════════════════════════════════════════════════════════
#  PARSER FILE .DB TIA PORTAL
# ══════════════════════════════════════════════════════════════════

_RE_ARRAY_VAL  = re.compile(
    r'([\w.]+)\[(\d+)\]\s*:=\s*([+-]?[\d]*\.?[\d]+(?:[eE][+-]?\d+)?)\s*;')
_RE_SCALAR_VAL = re.compile(r'^[ \t]*([\w.]+)\s*:=\s*([^;]+?)\s*;', re.MULTILINE)
_RE_BEGIN      = re.compile(r'\bBEGIN\b', re.IGNORECASE)


def _parse_db_body(text: str, result: dict) -> dict:
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
#  PLC OFFSET MAP - Fb936_ControlloSpessore_v0 (NON-ottimizzato)
# ══════════════════════════════════════════════════════════════════
# Calcolata sulla base della struttura SCL v0.2.
# Compatibile con DB istanza che riporta:
#   { DB_Accessible_From_OPC_UA := 'FALSE'; S7_Optimized_Access := 'FALSE' }

ARRAY_SIZE = 1000
PLC_REAL_SIZE  = 4
PLC_LREAL_SIZE = 8
PLC_INT_SIZE   = 2


class _OffsetBuilder:
    """Helper per costruire offset map rispettando le regole di allineamento
    S7 non-optimized (allineamento a 2 byte per tipi a più byte)."""
    def __init__(self):
        self.entries = []
        self.off = 0

    def align(self, n=2):
        if self.off % n:
            self.off += (n - self.off % n)

    def real(self, name):
        self.align(2)
        self.entries.append((name, self.off, 'real', PLC_REAL_SIZE))
        self.off += PLC_REAL_SIZE

    def lreal(self, name):
        self.align(2)
        self.entries.append((name, self.off, 'lreal', PLC_LREAL_SIZE))
        self.off += PLC_LREAL_SIZE

    def int16(self, name):
        self.align(2)
        self.entries.append((name, self.off, 'int', PLC_INT_SIZE))
        self.off += PLC_INT_SIZE

    def bools(self, names):
        self.align(2)
        base = self.off
        for i, n in enumerate(names):
            self.entries.append((n, base + i // 8, 'bool', i % 8))
        self.off = base + (len(names) - 1) // 8 + 1

    def array_real(self, name, n):
        self.align(2)
        self.entries.append((name, self.off, 'array_real', n))
        self.off += n * PLC_REAL_SIZE

    def array_int(self, name, n):
        self.align(2)
        self.entries.append((name, self.off, 'array_int', n))
        self.off += n * PLC_INT_SIZE

    def skip_rtrig(self):
        """R_TRIG/F_TRIG in DB non-ottim = 2 byte (CLK+Q+statBit packed)."""
        self.align(2)
        self.off += 2

    def skip_pointer(self):
        """VAR_IN_OUT puntatore = 6 byte."""
        self.align(2)
        self.off += 6

    def dati_buffer(self, n):
        """Dati[0..n] struct: Spessore + Pos + Speed (3 Real = 12 byte)."""
        self.align(2)
        for i in range(n + 1):
            self.entries.append((f'Dati[{i}].Spessore', self.off, 'real', PLC_REAL_SIZE))
            self.off += PLC_REAL_SIZE
            self.entries.append((f'Dati[{i}].Pos', self.off, 'real', PLC_REAL_SIZE))
            self.off += PLC_REAL_SIZE
            self.entries.append((f'Dati[{i}].Speed', self.off, 'real', PLC_REAL_SIZE))
            self.off += PLC_REAL_SIZE


def plc_build_offset_map():
    """Costruisce mappa offset per DB istanza Fb936_ControlloSpessore_v0."""
    b = _OffsetBuilder()

    # ═══ VAR_INPUT ═══════════════════════════════════════════════════
    # UDT I_ParametriCntrolloSpessore (34 byte)
    b.real('I_ParametriCntrolloSpessore.PosizioneCentroVentosa')
    b.real('I_ParametriCntrolloSpessore.RangeControllo')
    b.real('I_ParametriCntrolloSpessore.RangeRallenta')
    b.real('I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme')
    b.real('I_ParametriCntrolloSpessore.SpessoreMassimo')
    b.lreal('I_ParametriCntrolloSpessore.OvrTrasfertPerpassaggio')
    b.bools(['I_ParametriCntrolloSpessore.DisabilitaControllo',
             'I_ParametriCntrolloSpessore.AbilitaTaratura'])
    b.real('I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento')

    # Altri input
    b.real('I_Spessore_mm')
    b.bools(['I_InvertiLettura', 'I_PiecePresence'])
    b.lreal('I_ActPosition')       # ← LReal nel FB v0.2
    b.lreal('I_ActSpeed')           # ← LReal nel FB v0.2
    b.int16('I_DirLavoro')

    # ═══ VAR_OUTPUT ══════════════════════════════════════════════════
    b.bools(['O_SpessoreOk', 'O_SpessoreNOK', 'O_AlmControlloDisattivo',
             'O_TaraturaAttiva', 'O_TaraturaCompletata'])
    b.real('O_SpessoreMedio')
    b.real('O_SpessoreMax')
    b.real('O_DeltaMedio')
    b.real('O_DeltaMax')
    b.int16('O_nCelleProfilo')

    # ═══ VAR_IN_OUT (puntatori 6 byte) ═══════════════════════════════
    b.skip_pointer()   # IO_OvrAuto
    b.skip_pointer()   # IO_OvrMan

    # ═══ VAR ═════════════════════════════════════════════════════════
    # Pinza struct (22 byte)
    b.bools(['Pinza.InZonaControllo', 'Pinza.InZonaRallenta'])
    b.lreal('Pinza.OvrAutoOld')
    b.lreal('Pinza.OvrManOld')
    b.real('Pinza.nLettureConsecutive')

    # Baseline
    b.array_real('aBaseline', ARRAY_SIZE)
    b.bools(['baselineValida'])

    # R_TRIG / F_TRIG (2 byte cad.)
    b.skip_rtrig()  # Fp
    b.skip_rtrig()  # Fn
    b.skip_rtrig()  # FpSlowing
    b.skip_rtrig()  # FnSlowing
    b.skip_rtrig()  # FpTaratura

    b.bools(['DirOk', 'Ripeti'])
    b.int16('index')
    b.int16('AppMinIndexSurce')
    b.int16('AppMaxIndexSurce')
    b.int16('AppMaxIndex')

    # Dati[0..100] (101 * 12 byte = 1212 byte)
    b.dati_buffer(100)

    b.bools(['taraturaInCorso'])
    b.array_real('aSomRaw', ARRAY_SIZE)
    b.array_int('aNraw', ARRAY_SIZE)
    b.array_real('aSomCal', ARRAY_SIZE)
    b.array_int('aNcal', ARRAY_SIZE)
    b.array_real('aProfiloSpessore', ARRAY_SIZE)
    b.array_real('aProfiloDelta', ARRAY_SIZE)

    b.bools(['AppSpessoreOk', 'AppSpessoreNok'])
    b.real('AppSpessoreMedio')
    b.real('AppSpessoreMax')
    b.real('AppDeltaMedio')
    b.real('AppDeltaMax')
    b.int16('AppNcelleProfilo')

    return b.entries, b.off


# ── Decodifica byte → valori ──────────────────────────────
def plc_decode_real(d, o):  return struct.unpack('>f', d[o:o+4])[0]
def plc_decode_lreal(d, o): return struct.unpack('>d', d[o:o+8])[0]
def plc_decode_int(d, o):   return struct.unpack('>h', d[o:o+2])[0]
def plc_decode_bool(d, o, b): return bool(d[o] & (1 << b))


def plc_decode_array_real(d, o, n):
    return list(struct.unpack(f'>{n}f', d[o:o + n * 4]))


def plc_decode_array_int(d, o, n):
    return list(struct.unpack(f'>{n}h', d[o:o + n * 2]))


def plc_decode_db(raw, offset_map):
    result = {'scalars': {}, 'arrays': {}}
    for name, off, dtype, sz in offset_map:
        try:
            if dtype == 'real':       result['scalars'][name] = plc_decode_real(raw, off)
            elif dtype == 'lreal':    result['scalars'][name] = plc_decode_lreal(raw, off)
            elif dtype == 'int':      result['scalars'][name] = plc_decode_int(raw, off)
            elif dtype == 'bool':     result['scalars'][name] = plc_decode_bool(raw, off, sz)
            elif dtype == 'array_real':
                result['arrays'][name] = plc_decode_array_real(raw, off, sz)
            elif dtype == 'array_int':
                result['arrays'][name] = plc_decode_array_int(raw, off, sz)
        except (struct.error, IndexError):
            if dtype in ('array_real', 'array_int'):
                result['arrays'][name] = [0.0] * sz
            elif dtype == 'bool':  result['scalars'][name] = False
            elif dtype == 'int':   result['scalars'][name] = 0
            else:                  result['scalars'][name] = 0.0
    return result


def plc_generate_db_text(decoded, db_name="ControlloSpessore_Export"):
    """Genera testo .db TIA Portal dal decoded (per log/archivio)."""
    lines = [
        f'DATA_BLOCK "{db_name}"',
        "{ S7_Optimized_Access := 'FALSE' }",
        "VERSION : 0.2",
        "NON_RETAIN",
        '"Fb936_ControlloSpessore_v0"',
        "BEGIN",
    ]
    for name, val in decoded['scalars'].items():
        if isinstance(val, bool):
            lines.append(f"   {name} := {'TRUE' if val else 'FALSE'};")
        elif isinstance(val, int):
            lines.append(f"   {name} := {val};")
        else:
            lines.append(f"   {name} := {val:.7e};")
    for name, arr in decoded['arrays'].items():
        for i, v in enumerate(arr):
            lines.append(f"   {name}[{i}] := {v:.7e};")
    lines.append("END_DATA_BLOCK")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  PLC READER (Snap7)
# ══════════════════════════════════════════════════════════════════

class PLCReader:
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
                    f"3) PUT/GET abilitato")
            off += sz; reads += 1
            if callback and reads % 20 == 0:
                callback(off * 100 // total_size)
        return data


# ══════════════════════════════════════════════════════════════════
#  SQLITE ARCHIVIO
# ══════════════════════════════════════════════════════════════════

def sqlite_init(db_path):
    """Inizializza archivio SQLite: crea tabella se non esiste."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS acquisizioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
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
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON acquisizioni(timestamp DESC)")
    con.commit()
    return con


def sqlite_insert(con, decoded, db_number=0, is_taratura=False):
    sc = decoded['scalars']
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    raw_db = plc_generate_db_text(decoded, db_name=f"DB{db_number}_{ts}")
    cur = con.execute("""
        INSERT INTO acquisizioni
        (timestamp, db_number, spessore_medio, spessore_max,
         delta_medio, delta_max, n_celle, spessore_ok, spessore_nok,
         taratura, raw_db)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ts, db_number,
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


def sqlite_query_recent(con, limit=500, filtro=None):
    """Ritorna lista di tuple (id, timestamp, db_number, sp_medio, sp_max,
    delta_medio, delta_max, n_celle, ok, nok, taratura)."""
    q = """SELECT id, timestamp, db_number, spessore_medio, spessore_max,
                  delta_medio, delta_max, n_celle, spessore_ok, spessore_nok,
                  taratura
           FROM acquisizioni"""
    params = ()
    if filtro == 'ok':
        q += " WHERE spessore_ok=1 AND spessore_nok=0 AND taratura=0"
    elif filtro == 'nok':
        q += " WHERE spessore_nok=1"
    elif filtro == 'tar':
        q += " WHERE taratura=1"
    q += " ORDER BY timestamp DESC LIMIT ?"
    params = (limit,)
    return list(con.execute(q, params))


def sqlite_load_raw(con, row_id):
    """Recupera raw_db text di una riga."""
    r = con.execute("SELECT raw_db FROM acquisizioni WHERE id=?",
                    (row_id,)).fetchone()
    return r[0] if r else None


def sqlite_count(con):
    r = con.execute("SELECT COUNT(*) FROM acquisizioni").fetchone()
    return int(r[0]) if r else 0


def sqlite_delete(con, row_id):
    con.execute("DELETE FROM acquisizioni WHERE id=?", (row_id,))
    con.commit()


# ══════════════════════════════════════════════════════════════════
#  PALETTE
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
PROFILE_CLR  = "#79c0ff"
BASELINE_CLR = "#e3b341"
DELTA_CLR    = "#ff9070"
THRESHOLD_CLR= "#ff6e85"
PLC_CLR   = "#f0883e"
AUTOEXP_CLR = "#56d364"
TAR_CLR   = "#d2a8ff"


# ══════════════════════════════════════════════════════════════════
#  APPLICAZIONE
# ══════════════════════════════════════════════════════════════════

class ThicknessApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"◈ Thickness Profiler  {APP_RELEASE}  —  Fb936 v0.2")
        self.geometry("1420x900")
        self.minsize(1120, 700)
        self.configure(bg=DARK_BG)

        # Settings (INI nella cartella dell'app)
        self._cfg, self._cfg_path = load_settings()

        self.db_data = None

        # Auto-export state
        self._autoexp_running = False
        self._autoexp_timer_id = None
        self._autoexp_client = None
        self._autoexp_count_ok = 0
        self._autoexp_count_nok = 0
        self._autoexp_count_tar = 0
        self._autoexp_sql_con = None
        self._autoexp_prev_sentinel = None

        # Offset map pre-calcolata (pesante, una volta sola)
        self._offset_map, self._db_size = plc_build_offset_map()

        self._style()
        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._startup_checks)

    # ─── SETTINGS ────────────────────────────────────────────
    def _get_pv_settings(self):
        """Ritorna i valori correnti dalle variabili Tk (fallback a cfg)."""
        plc_ip = self._pv_plc_ip.get().strip() if hasattr(self, '_pv_plc_ip') else self._cfg['PLC']['ip']
        plc_rack = self._pv_plc_rack.get() if hasattr(self, '_pv_plc_rack') else self._cfg['PLC']['rack']
        plc_slot = self._pv_plc_slot.get() if hasattr(self, '_pv_plc_slot') else self._cfg['PLC']['slot']
        plc_db = self._pv_plc_db.get() if hasattr(self, '_pv_plc_db') else self._cfg['PLC']['db']
        sql_path = self._pv_sql_path.get() if hasattr(self, '_pv_sql_path') else self._cfg['SQL']['path']
        return plc_ip, plc_rack, plc_slot, plc_db, sql_path

    def _save_settings_to_ini(self):
        plc_ip, plc_rack, plc_slot, plc_db, sql_path = self._get_pv_settings()
        if 'PLC' not in self._cfg: self._cfg['PLC'] = {}
        if 'SQL' not in self._cfg: self._cfg['SQL'] = {}
        self._cfg['PLC']['ip']   = plc_ip
        self._cfg['PLC']['rack'] = str(plc_rack)
        self._cfg['PLC']['slot'] = str(plc_slot)
        self._cfg['PLC']['db']   = str(plc_db)
        self._cfg['SQL']['path'] = sql_path
        if save_settings(self._cfg, self._cfg_path):
            self.app_log(f"Settings salvate in {self._cfg_path}")
            return True
        else:
            self.app_log("Errore salvataggio settings", "warn")
            return False

    def _startup_checks(self):
        missing = []
        try: import numpy
        except ImportError: missing.append("numpy")
        try: import matplotlib
        except ImportError: missing.append("matplotlib")
        if missing:
            messagebox.showwarning("Librerie mancanti",
                f"Installare: {', '.join(missing)}\n\npip install {' '.join(missing)}")
        # Messaggio iniziale nel log
        if not os.path.isfile(self._cfg_path):
            self.app_log(f"Primo avvio: creazione settings in {self._cfg_path}")
            self._save_settings_to_ini()

    def _on_close(self):
        if self._autoexp_running:
            self._autoexp_stop()
        self._save_settings_to_ini()
        try: self.destroy()
        except Exception: pass
        _kill_process_tree()

    # ─── STILE ───────────────────────────────────────────────
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

    # ─── LAYOUT PRINCIPALE ───────────────────────────────────
    def _build_ui(self):
        top = ttk.Frame(self); top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(top, text="◈ THICKNESS PROFILER",
                  style="Title.TLabel").pack(side="left")
        tk.Label(top, text=APP_RELEASE, font=("Consolas", 9),
                 fg="#58a6ff", bg=DARK_BG, padx=8).pack(side="left")
        tk.Label(top, text=f"Fb936_ControlloSpessore_v0",
                 font=("Consolas", 9), fg=MUTED_CLR, bg=DARK_BG,
                 padx=8).pack(side="left")

        ttk.Button(top, text="📁 Apri file .db...",
                   style="Accent.TButton",
                   command=self._open_file).pack(side="right", padx=2)

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=10, pady=8)

        self._left_panel = ttk.Frame(main, width=340)
        self._left_panel.pack_propagate(False)
        main.add(self._left_panel, weight=0)

        right_panel = ttk.Frame(main)
        main.add(right_panel, weight=1)

        self._build_left(self._left_panel)
        self._build_right(right_panel)

        # Barra inferiore
        bot = ttk.Frame(self); bot.pack(fill="x", padx=10, pady=(0, 6))
        self._lbl_status = tk.Label(bot, text="Pronto.",
            bg=DARK_BG, fg=MUTED_CLR, font=("Consolas", 9), anchor="w")
        self._lbl_status.pack(side="left", fill="x", expand=True)
        self._lbl_file = tk.Label(bot, text="Nessun dato caricato",
            bg=DARK_BG, fg=ACCENT, font=("Consolas", 9, "bold"), anchor="e")
        self._lbl_file.pack(side="right")

    # ─── PANNELLO SINISTRO ───────────────────────────────────
    def _build_left(self, parent):
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

        # RISULTATO
        box_res = ttk.LabelFrame(inner, text="  RISULTATO ULTIMA PASSATA  ",
                                 padding=8)
        box_res.pack(fill="x", padx=4, pady=4)
        self._pv_verdict = tk.StringVar(value="—")
        self._lbl_verdict = tk.Label(box_res, textvariable=self._pv_verdict,
                 bg=DARK_BG, font=("Consolas", 18, "bold"),
                 fg=MUTED_CLR)
        self._lbl_verdict.pack(pady=4)

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

        # TARATURA
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

        # PARAMETRI UDT
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

        # STATO PINZA
        box_pz = ttk.LabelFrame(inner, text="  STATO PINZA  ", padding=6)
        box_pz.pack(fill="x", padx=4, pady=4)
        self._pv_piece = tk.StringVar(value="—")
        self._pv_zona_ctrl = tk.StringVar(value="—")
        self._pv_zona_rall = tk.StringVar(value="—")
        self._pv_pos = tk.StringVar(value="—")
        self._pv_speed = tk.StringVar(value="—")
        for lbl, var in [("Pezzo presente:", self._pv_piece),
                         ("In zona ctrl:", self._pv_zona_ctrl),
                         ("In zona rall:", self._pv_zona_rall),
                         ("Posizione [mm]:", self._pv_pos),
                         ("Velocità:", self._pv_speed)]:
            row = ttk.Frame(box_pz); row.pack(fill="x", pady=1)
            tk.Label(row, text=lbl, bg=DARK_BG, fg=MUTED_CLR,
                     font=("Consolas", 9), width=16, anchor="w").pack(side="left")
            tk.Label(row, textvariable=var, bg=DARK_BG, fg=TEXT_CLR,
                     font=("Consolas", 9, "bold"), anchor="e").pack(side="right")

    # ─── PANNELLO DESTRO ──────────────────────────────────────
    def _build_right(self, parent):
        self._nb = ttk.Notebook(parent)
        self._nb.pack(fill="both", expand=True)

        t_viewer = ttk.Frame(self._nb); self._nb.add(t_viewer, text="  📊 Profilo  ")
        self._build_viewer_tab(t_viewer)

        t_delta = ttk.Frame(self._nb); self._nb.add(t_delta, text="  📈 Delta  ")
        self._build_delta_tab(t_delta)

        t_plc = ttk.Frame(self._nb); self._nb.add(t_plc, text="  🔌 PLC Reader  ")
        self._build_plc_tab(t_plc)

        t_auto = ttk.Frame(self._nb); self._nb.add(t_auto, text="  ⚡ Auto-Export  ")
        self._build_autoexp_tab(t_auto)

        t_hist = ttk.Frame(self._nb); self._nb.add(t_hist, text="  📚 History  ")
        self._build_history_tab(t_hist)

        t_cfg = ttk.Frame(self._nb); self._nb.add(t_cfg, text="  ⚙ Impostazioni  ")
        self._build_settings_tab(t_cfg)

    # ══════════════════════════════════════════════════════════
    #  TAB 1 — VIEWER PROFILO
    # ══════════════════════════════════════════════════════════
    def _build_viewer_tab(self, parent):
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
                (self._pv_show_samples,   "Buffer Dati"),
                ]:
            tk.Checkbutton(bar, text=text, variable=var,
                bg=DARK_BG, fg=TEXT_CLR, selectcolor="#1f6feb",
                activebackground=DARK_BG, font=("Consolas", 9),
                command=self._draw_viewer).pack(side="left", padx=4)

        ttk.Button(bar, text="🔄 Refresh",
                   command=self._draw_all).pack(side="right", padx=2)
        ttk.Button(bar, text="💾 PNG",
                   command=lambda: self._save_plot(self.fig_viewer)
                   ).pack(side="right", padx=2)

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

    def _draw_all(self):
        self._draw_viewer()
        self._draw_delta()

    def _draw_viewer(self):
        ax = self.ax_viewer
        ax.clear()
        self._stylize_ax(ax)

        if self.db_data is None:
            ax.text(0.5, 0.5, "Nessun dato — leggi dal PLC o apri un file .db",
                    ha='center', va='center', color=MUTED_CLR,
                    fontsize=12, transform=ax.transAxes)
            self._viewer_canvas.draw_idle()
            return

        arrays = self.db_data.get('arrays', {})
        scalars = self.db_data.get('scalars', {})

        range_ctrl = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.RangeControllo', 'RangeControllo', default=150.0)
        pos_ctr = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.PosizioneCentroVentosa', 'PosizioneCentroVentosa', default=630.0)
        soglia  = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.SpessoreMassimo', 'SpessoreMassimo', default=1.5)
        sp_rif  = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento', 'SpessoreDiscoRiferimento', default=1.0)

        n = ARRAY_SIZE
        x_mm = np.linspace(pos_ctr - range_ctrl, pos_ctr + range_ctrl, n)

        prof = arrays.get('aProfiloSpessore', [])
        if len(prof) >= n and self._pv_show_profile.get():
            p = np.array(prof[:n])
            mask = np.abs(p) > 1e-6
            if mask.any():
                ax.plot(x_mm[mask], p[mask], color=PROFILE_CLR, lw=1.5,
                        label='Profilo spessore [mm]', zorder=5)

        bas = arrays.get('aBaseline', [])
        if len(bas) >= n and self._pv_show_baseline.get():
            b = np.array(bas[:n])
            mask = np.abs(b) > 1e-6
            if mask.any():
                ax.plot(x_mm[mask], b[mask], color=BASELINE_CLR, lw=1.0,
                        ls='--', alpha=0.8, label='Baseline (offset macchina)')

        dlt = arrays.get('aProfiloDelta', [])
        if len(dlt) >= n and self._pv_show_delta.get():
            d = np.array(dlt[:n])
            mask = np.abs(d) > 1e-6
            if mask.any():
                ax.plot(x_mm[mask], d[mask], color=DELTA_CLR, lw=1.2,
                        alpha=0.8, label=f'Delta vs riferimento ({sp_rif:.2f}mm)')

        if self._pv_show_threshold.get():
            ax.axhline(soglia, color=THRESHOLD_CLR, lw=1.0, ls=':',
                       alpha=0.9, label=f'Soglia doppio ({soglia:.2f}mm)')

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
                           label=f'Buffer Dati ({len(samples_x)} pt)',
                           zorder=7)

        ax.set_xlabel("Posizione asse [mm]", color=TEXT_CLR, fontsize=10)
        ax.set_ylabel("Spessore [mm]", color=TEXT_CLR, fontsize=10)

        title = self.db_data.get('filename', '—')
        ts = self.db_data.get('loaded_at')
        if ts:
            title = f"{title}   •   {ts.strftime('%H:%M:%S')}"
        ax.set_title(title, color=ACCENT, fontsize=10, pad=6)

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
                try: return float(scalars[k])
                except (TypeError, ValueError): pass
        return default

    # ══════════════════════════════════════════════════════════
    #  TAB 2 — DELTA
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
        range_ctrl = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.RangeControllo', 'RangeControllo', default=150.0)
        pos_ctr = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.PosizioneCentroVentosa', 'PosizioneCentroVentosa', default=630.0)
        sp_rif  = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento', 'SpessoreDiscoRiferimento', default=1.0)
        soglia  = self._get_scalar(scalars, 'I_ParametriCntrolloSpessore.SpessoreMassimo', 'SpessoreMassimo', default=1.5)

        n = ARRAY_SIZE
        x_mm = np.linspace(pos_ctr - range_ctrl, pos_ctr + range_ctrl, n)
        dlt = arrays.get('aProfiloDelta', [])

        if len(dlt) >= n:
            d = np.array(dlt[:n])
            mask = np.abs(d) > 1e-6
            if mask.any():
                x_m = x_mm[mask]; d_m = d[mask]
                ax.fill_between(x_m, 0, d_m, where=(d_m >= 0),
                                color=ERR_CLR, alpha=0.35,
                                label='Eccesso (più spesso del riferimento)')
                ax.fill_between(x_m, 0, d_m, where=(d_m < 0),
                                color=ACCENT, alpha=0.35,
                                label='Difetto (più sottile del riferimento)')
                ax.plot(x_m, d_m, color=DELTA_CLR, lw=1.2)

        ax.axhline(0, color=MUTED_CLR, lw=1.0, ls='-', alpha=0.6)
        allarme = soglia - sp_rif
        ax.axhline(allarme, color=THRESHOLD_CLR, lw=1.0, ls=':',
                   alpha=0.9, label=f'Soglia allarme (Δ={allarme:.2f}mm)')

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
            self._snap7_missing_msg(parent); return

        top = ttk.LabelFrame(parent, text="  Connessione S7-1500/1200  ", padding=8)
        top.pack(fill="x", padx=8, pady=6)

        self._pv_plc_ip   = tk.StringVar(value=self._cfg['PLC'].get('ip',   '192.168.0.1'))
        self._pv_plc_rack = tk.StringVar(value=self._cfg['PLC'].get('rack', '0'))
        self._pv_plc_slot = tk.StringVar(value=self._cfg['PLC'].get('slot', '1'))
        self._pv_plc_db   = tk.StringVar(value=self._cfg['PLC'].get('db',   '16010'))

        r1 = ttk.Frame(top); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="IP:", width=6).pack(side="left")
        ttk.Entry(r1, textvariable=self._pv_plc_ip, width=16,
                  font=("Consolas", 10)).pack(side="left", padx=2)
        ttk.Label(r1, text="Rack:", width=6).pack(side="left", padx=(10, 0))
        ttk.Entry(r1, textvariable=self._pv_plc_rack, width=4).pack(side="left")
        ttk.Label(r1, text="Slot:", width=6).pack(side="left", padx=(10, 0))
        ttk.Entry(r1, textvariable=self._pv_plc_slot, width=4).pack(side="left")
        ttk.Label(r1, text="DB #:", width=6).pack(side="left", padx=(10, 0))
        ttk.Entry(r1, textvariable=self._pv_plc_db, width=8).pack(side="left")

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
        self._btn_plc_load_viewer = ttk.Button(r2, text="📊 Carica nel Viewer",
            command=self._plc_load_viewer, state="disabled")
        self._btn_plc_load_viewer.pack(side="left", padx=2)

        self._pv_plc_status = tk.StringVar(value="● Disconnesso")
        tk.Label(r2, textvariable=self._pv_plc_status,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9, "bold")).pack(side="right", padx=4)

        # Info DB
        info = ttk.LabelFrame(parent, text="  Info struttura DB  ", padding=6)
        info.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(info,
                 text=f"Dimensione DB istanza: {self._db_size} byte  •  "
                      f"Scalari: {sum(1 for e in self._offset_map if e[2] in ('real','lreal','int','bool'))}  •  "
                      f"Array: {sum(1 for e in self._offset_map if e[2] in ('array_real','array_int'))}",
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9)).pack(anchor="w")

        # Log
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

        self._plc_log_msg("=== PLC Reader ===\n", "info")
        self._plc_log_msg(f"Settings caricate da: {self._cfg_path}\n")
        self._plc_log_msg("Configura IP/DB e premi Connetti.\n\n")

        self._plc_client = None
        self._plc_last_decoded = None

    def _snap7_missing_msg(self, parent):
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

    def _plc_log_msg(self, msg, tag=""):
        if hasattr(self, '_plc_log'):
            self._plc_log.insert("end", msg, tag)
            self._plc_log.see("end")

    def _plc_connect(self):
        try:
            ip = self._pv_plc_ip.get().strip()
            rack = int(self._pv_plc_rack.get() or 0)
            slot = int(self._pv_plc_slot.get() or 1)
            self._plc_log_msg(f"→ Connessione a {ip} (rack={rack}, slot={slot})...\n", "info")
            self._plc_client = PLCReader(ip, rack, slot)
            cpu, pdu = self._plc_client.connect()
            self._plc_log_msg(f"✓ Connesso — CPU: {cpu}, PDU: {pdu} byte\n", "ok")
            self._pv_plc_status.set("● Connesso")
            self._btn_plc_connect.config(state="disabled")
            self._btn_plc_disconnect.config(state="normal")
            self._btn_plc_read.config(state="normal")
            # Auto-save settings on connect success
            self._save_settings_to_ini()
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
        self._btn_plc_load_viewer.config(state="disabled")

    def _plc_read_now(self):
        if not self._plc_client:
            return
        try:
            db_num = int(self._pv_plc_db.get())
            self._plc_log_msg(f"→ Lettura DB{db_num} ({self._db_size} byte)...\n", "info")
            t0 = time.time()
            raw = self._plc_client.read_db_raw(db_num, self._db_size)
            dt = (time.time() - t0) * 1000
            self._plc_log_msg(f"✓ {self._db_size} byte letti in {dt:.0f} ms\n", "ok")

            decoded = plc_decode_db(raw, self._offset_map)
            self._plc_last_decoded = decoded
            sc = decoded['scalars']
            self._plc_log_msg(
                f"  Sp.medio={sc.get('AppSpessoreMedio',0):.3f} "
                f"Sp.max={sc.get('AppSpessoreMax',0):.3f} "
                f"ΔMax={sc.get('AppDeltaMax',0):.3f} "
                f"n={int(sc.get('AppNcelleProfilo',0))}\n", "ok")
            if sc.get('AppSpessoreNok'):
                self._plc_log_msg("  ⚠ DOPPIO SPESSORE RILEVATO\n", "err")
            if sc.get('O_TaraturaAttiva'):
                self._plc_log_msg("  ⚙ Taratura in corso\n", "info")
            self._btn_plc_load_viewer.config(state="normal")
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
    #  TAB 4 — AUTO-EXPORT (solo SQLite)
    # ══════════════════════════════════════════════════════════
    def _build_autoexp_tab(self, parent):
        if not SNAP7_AVAILABLE:
            self._snap7_missing_msg(parent); return

        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, width=380); pane.add(left, weight=0)
        left.pack_propagate(False)
        right = ttk.Frame(pane); pane.add(right, weight=1)

        # Configurazione
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
            width=30, state="readonly",
            values=["Variazione AppSpessoreMedio",
                    "O_TaraturaCompletata ↑",
                    "Falling edge Pinza.InZonaControllo"])
        cmb.current(0); cmb.pack(side="left", padx=2)

        # Percorso SQL (solo visualizzazione, si modifica in Impostazioni)
        lf2 = ttk.LabelFrame(left, text="  Archivio SQLite  ", padding=6)
        lf2.pack(fill="x", padx=6, pady=4)
        tk.Label(lf2, text="Percorso (da Impostazioni):",
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 8)).pack(anchor="w")
        self._pv_autoexp_sql_shown = tk.StringVar(value="—")
        tk.Label(lf2, textvariable=self._pv_autoexp_sql_shown,
                 bg=DARK_BG, fg=ACCENT,
                 font=("Consolas", 9), wraplength=350,
                 anchor="w", justify="left").pack(anchor="w", pady=2)

        # Opzioni
        lf3 = ttk.LabelFrame(left, text="  Opzioni  ", padding=6)
        lf3.pack(fill="x", padx=6, pady=4)
        self._pv_autoexp_load_viewer = tk.BooleanVar(value=True)
        self._pv_autoexp_save_tar = tk.BooleanVar(value=True)
        tk.Checkbutton(lf3, text="Carica automaticamente nel Viewer",
            variable=self._pv_autoexp_load_viewer,
            bg=DARK_BG, fg=TEXT_CLR, selectcolor="#1f6feb",
            activebackground=DARK_BG, font=("Consolas", 9),
            anchor="w").pack(fill="x", pady=1)
        tk.Checkbutton(lf3, text="Archivia anche le tarature",
            variable=self._pv_autoexp_save_tar,
            bg=DARK_BG, fg=TEXT_CLR, selectcolor="#1f6feb",
            activebackground=DARK_BG, font=("Consolas", 9),
            anchor="w").pack(fill="x", pady=1)

        # Monitoraggio
        lf4 = ttk.LabelFrame(left, text="  Monitoraggio  ", padding=6)
        lf4.pack(fill="x", padx=6, pady=4)
        r5 = ttk.Frame(lf4); r5.pack(fill="x")
        self._btn_autoexp_start = tk.Button(r5, text="▶ Avvia",
            bg=OK_CLR, fg=DARK_BG, font=("Consolas", 10, "bold"),
            command=self._autoexp_start, width=10)
        self._btn_autoexp_start.pack(side="left", padx=2)
        self._btn_autoexp_stop = tk.Button(r5, text="■ Stop",
            bg=ERR_CLR, fg=DARK_BG, font=("Consolas", 10, "bold"),
            command=self._autoexp_stop, state="disabled", width=10)
        self._btn_autoexp_stop.pack(side="left", padx=2)

        self._pv_autoexp_status = tk.StringVar(value="● Fermo")
        tk.Label(lf4, textvariable=self._pv_autoexp_status,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 10, "bold")).pack(anchor="w", pady=4)

        self._pv_autoexp_count = tk.StringVar(value="✓ 0  ✗ 0  ⚙ 0")
        tk.Label(lf4, textvariable=self._pv_autoexp_count,
                 bg=DARK_BG, fg=AUTOEXP_CLR,
                 font=("Consolas", 11, "bold")).pack(anchor="w")
        self._pv_autoexp_last = tk.StringVar(value="")
        tk.Label(lf4, textvariable=self._pv_autoexp_last,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 8)).pack(anchor="w")

        self._pv_autoexp_sql_count = tk.StringVar(value="DB: — righe")
        tk.Label(lf4, textvariable=self._pv_autoexp_sql_count,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 8)).pack(anchor="w", pady=(4, 0))

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
                            ("tar", TAR_CLR)]:
            self._ae_log.tag_config(tag, foreground=color)
        self._ae_log.insert("end", "=== Auto-Export ===\n", "info")
        self._ae_log.insert("end",
            "Connettiti al PLC (tab PLC Reader), poi premi Avvia.\n\n")

        # Aggiorna label SQL path
        self._update_autoexp_sql_label()

    def _update_autoexp_sql_label(self):
        """Aggiorna label con path SQL corrente (risolto)."""
        if hasattr(self, '_pv_sql_path'):
            raw = self._pv_sql_path.get()
        else:
            raw = self._cfg['SQL'].get('path', 'thickness_archive.sqlite')
        resolved = resolve_sql_path(raw)
        if hasattr(self, '_pv_autoexp_sql_shown'):
            self._pv_autoexp_sql_shown.set(resolved)

    def _ae_log_msg(self, msg, tag=""):
        if hasattr(self, '_ae_log'):
            self._ae_log.insert("end", msg, tag)
            self._ae_log.see("end")

    def _autoexp_start(self):
        if not hasattr(self, '_plc_client') or not self._plc_client:
            messagebox.showwarning("PLC non connesso",
                "Connettiti al PLC prima di avviare l'Auto-Export.")
            self._nb.select(2)
            return

        sql_path = resolve_sql_path(self._pv_sql_path.get())
        try:
            self._autoexp_sql_con = sqlite_init(sql_path)
            n0 = sqlite_count(self._autoexp_sql_con)
            self._ae_log_msg(f"✓ SQLite aperto: {sql_path}\n", "ok")
            self._ae_log_msg(f"  Archivio esistente: {n0} righe\n", "info")
            self._pv_autoexp_sql_count.set(f"DB: {n0} righe")
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
            f"▶ Avvio — poll {self._pv_autoexp_poll.get()}ms — "
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
        if not self._autoexp_running:
            return

        try:
            db_num = int(self._pv_plc_db.get())
            raw = self._autoexp_client.read_db_raw(db_num, self._db_size, chunk=900)
            decoded = plc_decode_db(raw, self._offset_map)
            sc = decoded['scalars']

            trig = self._pv_autoexp_trig.get()
            if trig.startswith("Variazione"):
                sentinel = round(float(sc.get('AppSpessoreMedio', 0.0)), 6)
            elif trig.startswith("O_TaraturaCompletata"):
                sentinel = bool(sc.get('O_TaraturaCompletata', False))
            else:
                sentinel = bool(sc.get('Pinza.InZonaControllo', False))

            triggered = False
            if self._autoexp_prev_sentinel is None:
                self._autoexp_prev_sentinel = sentinel
            else:
                if trig.startswith("Falling edge"):
                    triggered = (self._autoexp_prev_sentinel and not sentinel)
                elif trig.startswith("O_TaraturaCompletata"):
                    triggered = (not self._autoexp_prev_sentinel and sentinel)
                else:
                    triggered = (sentinel != self._autoexp_prev_sentinel)
                self._autoexp_prev_sentinel = sentinel

            if triggered:
                self._autoexp_on_trigger(decoded, db_num)

        except Exception as e:
            self._ae_log_msg(f"✗ Errore polling: {e}\n", "err")
            self._pv_autoexp_status.set("● Errore polling")

        if self._autoexp_running:
            try:
                ms = max(20, int(self._pv_autoexp_poll.get()))
            except ValueError:
                ms = 100
            self._autoexp_timer_id = self.after(ms, self._autoexp_poll)

    def _autoexp_on_trigger(self, decoded, db_num):
        sc = decoded['scalars']
        ts = datetime.datetime.now()

        is_taratura = bool(sc.get('O_TaraturaAttiva', False)) or \
                      self._pv_autoexp_trig.get().startswith("O_TaraturaCompletata")
        is_nok = bool(sc.get('AppSpessoreNok', sc.get('O_SpessoreNOK', False)))

        # Se taratura e opzione disabilitata: skip
        if is_taratura and not self._pv_autoexp_save_tar.get():
            return

        if is_taratura:
            prefix = "TAR"
            self._autoexp_count_tar += 1
            tag = "tar"
        elif is_nok:
            prefix = "NOK"
            self._autoexp_count_nok += 1
            tag = "err"
        else:
            prefix = "OK "
            self._autoexp_count_ok += 1
            tag = "ok"

        # Insert in SQLite
        try:
            rid = sqlite_insert(self._autoexp_sql_con, decoded,
                                 db_num, is_taratura)
        except Exception as e:
            self._ae_log_msg(f"✗ err SQLite: {e}\n", "err")
            return

        # Log
        icon = "⚙" if is_taratura else ("✗" if is_nok else "✓")
        sp_med = sc.get('AppSpessoreMedio', 0)
        d_max  = sc.get('AppDeltaMax', 0)
        n_cel  = int(sc.get('AppNcelleProfilo', 0))

        self._ae_log_msg(
            f"{icon} {prefix} [{ts.strftime('%H:%M:%S')}] "
            f"Sp.med={sp_med:.3f}  ΔMax={d_max:.3f}  n={n_cel:>3d}  "
            f"#{rid}\n", tag)

        # Aggiorna UI
        self._pv_autoexp_count.set(
            f"✓ {self._autoexp_count_ok}  "
            f"✗ {self._autoexp_count_nok}  "
            f"⚙ {self._autoexp_count_tar}")
        self._pv_autoexp_last.set(
            f"{icon} {ts.strftime('%H:%M:%S')}  #{rid}")
        try:
            n_total = sqlite_count(self._autoexp_sql_con)
            self._pv_autoexp_sql_count.set(f"DB: {n_total} righe")
        except Exception: pass

        # Carica nel viewer
        if self._pv_autoexp_load_viewer.get():
            try:
                db_name = f"DB{db_num}_#{rid}_{prefix}"
                text = plc_generate_db_text(decoded, db_name=db_name)
                data = parse_db_file_from_text(text, filename=f"#{rid}")
                self._load_data(data)
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════
    #  TAB 5 — HISTORY (query SQLite)
    # ══════════════════════════════════════════════════════════
    def _build_history_tab(self, parent):
        bar = ttk.Frame(parent); bar.pack(fill="x", padx=6, pady=6)

        ttk.Label(bar, text="Filtro:").pack(side="left")
        self._pv_hist_filter = tk.StringVar(value="Tutti")
        cmb = ttk.Combobox(bar, textvariable=self._pv_hist_filter,
            width=15, state="readonly",
            values=["Tutti", "Solo OK", "Solo NOK", "Solo Tarature"])
        cmb.current(0); cmb.pack(side="left", padx=4)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._history_refresh())

        ttk.Label(bar, text="Limite:").pack(side="left", padx=(10, 2))
        self._pv_hist_limit = tk.StringVar(value="500")
        ttk.Entry(bar, textvariable=self._pv_hist_limit, width=6
                  ).pack(side="left", padx=2)

        ttk.Button(bar, text="🔄 Aggiorna",
                   command=self._history_refresh).pack(side="left", padx=4)

        self._pv_hist_info = tk.StringVar(value="0 righe")
        tk.Label(bar, textvariable=self._pv_hist_info,
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9)).pack(side="right", padx=6)

        # Lista
        lf = ttk.LabelFrame(parent, text="  Archivio  ", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=6)

        cols = ("id", "time", "db", "verdict", "medio", "max", "delta_med", "delta_max", "n")
        tree = ttk.Treeview(lf, columns=cols, show="headings", height=22)
        for c, txt, w, anc in [
                ("id", "ID", 60, "e"),
                ("time", "Timestamp", 170, "w"),
                ("db", "DB#", 60, "e"),
                ("verdict", "Esito", 80, "center"),
                ("medio", "Sp. medio", 85, "e"),
                ("max", "Sp. max", 85, "e"),
                ("delta_med", "Δ medio", 85, "e"),
                ("delta_max", "Δ max", 85, "e"),
                ("n", "N. celle", 65, "e")]:
            tree.heading(c, text=txt)
            tree.column(c, width=w, anchor=anc)
        tree.tag_configure('ok', foreground=OK_CLR)
        tree.tag_configure('nok', foreground=ERR_CLR)
        tree.tag_configure('tar', foreground=TAR_CLR)

        sb = ttk.Scrollbar(lf, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda e: self._history_open_selected())
        self._hist_tree = tree

        # Bottoni
        bar2 = ttk.Frame(parent); bar2.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar2, text="📊 Apri nel Viewer",
                   style="Accent.TButton",
                   command=self._history_open_selected).pack(side="left", padx=2)
        ttk.Button(bar2, text="🗑 Elimina selezionato",
                   command=self._history_delete_selected).pack(side="left", padx=2)

    def _history_refresh(self):
        sql_path = resolve_sql_path(self._pv_sql_path.get()
            if hasattr(self, '_pv_sql_path') else self._cfg['SQL']['path'])
        if not os.path.isfile(sql_path):
            self._hist_tree.delete(*self._hist_tree.get_children())
            self._pv_hist_info.set("archivio non trovato")
            return
        try:
            con = sqlite3.connect(sql_path)
            # Mappa filtro UI → query
            filt_map = {"Tutti": None, "Solo OK": 'ok',
                        "Solo NOK": 'nok', "Solo Tarature": 'tar'}
            filt = filt_map.get(self._pv_hist_filter.get())
            try:    limit = int(self._pv_hist_limit.get())
            except ValueError: limit = 500
            rows = sqlite_query_recent(con, limit=limit, filtro=filt)
            con.close()
        except Exception as e:
            messagebox.showerror("Errore query", str(e))
            return

        self._hist_tree.delete(*self._hist_tree.get_children())
        for r in rows:
            rid, ts, dbn, sm, smx, dm, dmx, n, ok, nok, tar = r
            if tar:
                verdict = "TARAT"; tag = 'tar'
            elif nok:
                verdict = "NOK";   tag = 'nok'
            elif ok:
                verdict = "OK";    tag = 'ok'
            else:
                verdict = "—";     tag = ''
            self._hist_tree.insert("", "end",
                values=(rid, ts, dbn, verdict,
                        f"{sm:.3f}", f"{smx:.3f}",
                        f"{dm:.3f}", f"{dmx:.3f}", n),
                tags=(tag,), iid=str(rid))
        self._pv_hist_info.set(f"{len(rows)} righe")

    def _history_open_selected(self):
        sel = self._hist_tree.selection()
        if not sel: return
        rid = int(sel[0])
        sql_path = resolve_sql_path(self._pv_sql_path.get()
            if hasattr(self, '_pv_sql_path') else self._cfg['SQL']['path'])
        try:
            con = sqlite3.connect(sql_path)
            text = sqlite_load_raw(con, rid)
            con.close()
            if not text:
                messagebox.showwarning("Vuoto", f"Riga #{rid} senza dati raw.")
                return
            data = parse_db_file_from_text(text, filename=f"#{rid}")
            self._load_data(data)
            self._nb.select(0)
        except Exception as e:
            messagebox.showerror("Errore", str(e))

    def _history_delete_selected(self):
        sel = self._hist_tree.selection()
        if not sel: return
        if not messagebox.askyesno("Conferma",
                f"Eliminare {len(sel)} righe dall'archivio?"):
            return
        sql_path = resolve_sql_path(self._pv_sql_path.get()
            if hasattr(self, '_pv_sql_path') else self._cfg['SQL']['path'])
        try:
            con = sqlite3.connect(sql_path)
            for iid in sel:
                sqlite_delete(con, int(iid))
            con.close()
        except Exception as e:
            messagebox.showerror("Errore", str(e))
            return
        self._history_refresh()

    # ══════════════════════════════════════════════════════════
    #  TAB 6 — IMPOSTAZIONI
    # ══════════════════════════════════════════════════════════
    def _build_settings_tab(self, parent):
        wrap = ttk.Frame(parent); wrap.pack(fill="both", expand=True, padx=12, pady=12)

        ttk.Label(wrap, text="Impostazioni applicazione",
                  style="Title.TLabel").pack(anchor="w", pady=(0, 8))

        # Location settings file
        info_lf = ttk.LabelFrame(wrap, text="  File di setup  ", padding=8)
        info_lf.pack(fill="x", pady=4)
        tk.Label(info_lf, text="Percorso:",
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9)).grid(row=0, column=0, sticky="w", padx=2, pady=2)
        tk.Label(info_lf, text=self._cfg_path,
                 bg=DARK_BG, fg=ACCENT,
                 font=("Consolas", 9), wraplength=600, justify="left",
                 anchor="w").grid(row=0, column=1, sticky="w", padx=4, pady=2)
        tk.Label(info_lf, text="Cartella app:",
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 9)).grid(row=1, column=0, sticky="w", padx=2, pady=2)
        tk.Label(info_lf, text=get_app_dir(),
                 bg=DARK_BG, fg=TEXT_CLR,
                 font=("Consolas", 9), wraplength=600, justify="left",
                 anchor="w").grid(row=1, column=1, sticky="w", padx=4, pady=2)

        # PLC
        plc_lf = ttk.LabelFrame(wrap, text="  PLC (Snap7)  ", padding=8)
        plc_lf.pack(fill="x", pady=6)

        # Importa le stesse StringVar usate nel tab PLC Reader (se già esistono)
        # Se non esistono ancora (es. snap7 non installato), creale qui
        if not hasattr(self, '_pv_plc_ip'):
            self._pv_plc_ip   = tk.StringVar(value=self._cfg['PLC'].get('ip',   '192.168.0.1'))
            self._pv_plc_rack = tk.StringVar(value=self._cfg['PLC'].get('rack', '0'))
            self._pv_plc_slot = tk.StringVar(value=self._cfg['PLC'].get('slot', '1'))
            self._pv_plc_db   = tk.StringVar(value=self._cfg['PLC'].get('db',   '16010'))

        for r, (lbl, var, w) in enumerate([
                ("IP indirizzo:", self._pv_plc_ip, 20),
                ("Rack:", self._pv_plc_rack, 6),
                ("Slot:", self._pv_plc_slot, 6),
                ("DB instance #:", self._pv_plc_db, 10),
                ]):
            tk.Label(plc_lf, text=lbl, bg=DARK_BG, fg=MUTED_CLR,
                     font=("Consolas", 10), anchor="w", width=16
                     ).grid(row=r, column=0, sticky="w", padx=2, pady=2)
            ttk.Entry(plc_lf, textvariable=var, width=w, font=("Consolas", 10)
                      ).grid(row=r, column=1, sticky="w", padx=4, pady=2)

        # SQL
        sql_lf = ttk.LabelFrame(wrap, text="  Archivio SQLite  ", padding=8)
        sql_lf.pack(fill="x", pady=6)

        self._pv_sql_path = tk.StringVar(
            value=self._cfg['SQL'].get('path', 'thickness_archive.sqlite'))
        tk.Label(sql_lf, text="File SQLite:",
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 10), anchor="w", width=16
                 ).grid(row=0, column=0, sticky="w", padx=2, pady=2)
        ttk.Entry(sql_lf, textvariable=self._pv_sql_path, width=60,
                  font=("Consolas", 10)
                  ).grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(sql_lf, text="📁",
                   command=lambda: self._browse_sql()
                   ).grid(row=0, column=2, padx=2)

        tk.Label(sql_lf,
                 text="(relativo = rispetto alla cartella app; assoluto = path completo)",
                 bg=DARK_BG, fg=MUTED_CLR,
                 font=("Consolas", 8)).grid(row=1, column=0, columnspan=3,
                                             sticky="w", padx=2, pady=(2, 4))

        self._pv_sql_resolved = tk.StringVar(
            value=f"Percorso risolto: {resolve_sql_path(self._pv_sql_path.get())}")
        tk.Label(sql_lf, textvariable=self._pv_sql_resolved,
                 bg=DARK_BG, fg=ACCENT, font=("Consolas", 9),
                 wraplength=700, anchor="w", justify="left"
                 ).grid(row=2, column=0, columnspan=3, sticky="w", padx=2, pady=2)

        # Aggiorna path risolto quando cambia
        self._pv_sql_path.trace_add('write', lambda *a: self._refresh_sql_resolved())

        # Bottoni azione
        btn_lf = ttk.Frame(wrap); btn_lf.pack(fill="x", pady=12)
        ttk.Button(btn_lf, text="💾 Salva impostazioni",
                   style="Accent.TButton",
                   command=self._save_and_confirm).pack(side="left", padx=2)
        ttk.Button(btn_lf, text="📂 Apri cartella app",
                   command=lambda: self._open_path(get_app_dir())
                   ).pack(side="left", padx=2)

        # Note tecniche
        notes_lf = ttk.LabelFrame(wrap, text="  Note  ", padding=8)
        notes_lf.pack(fill="both", expand=True, pady=4)
        notes = tk.Text(notes_lf, bg=PANEL_BG, fg=TEXT_CLR,
            font=("Consolas", 9), wrap="word", height=10,
            insertbackground=TEXT_CLR)
        notes.pack(fill="both", expand=True)
        notes.insert("end", f"""
Fb936_ControlloSpessore_v0 — S7_Optimized_Access := 'FALSE'
Dimensione DB istanza attesa: {self._db_size} byte

CONFIGURAZIONE CPU
 • Abilitare "Consenti PUT/GET da partner remoto" nelle proprietà CPU
 • Nessun password utente impostato sul DB

TRIGGER AUTO-EXPORT
 • Variazione AppSpessoreMedio: salva quando il valore cambia
 • O_TaraturaCompletata ↑: salva al completamento taratura
 • Falling edge Pinza.InZonaControllo: salva a fine passata (più preciso)

ARCHIVIO SQLITE
 • Tabella 'acquisizioni' con tutti gli scalari di sintesi + raw_db completo
 • La colonna raw_db contiene il .db TIA Portal ricostruito per ogni passata
 • Doppio click su una riga in History per ricaricarla nel Viewer
""")
        notes.config(state="disabled")

    def _refresh_sql_resolved(self):
        try:
            self._pv_sql_resolved.set(
                f"Percorso risolto: {resolve_sql_path(self._pv_sql_path.get())}")
            self._update_autoexp_sql_label()
        except Exception: pass

    def _save_and_confirm(self):
        if self._save_settings_to_ini():
            messagebox.showinfo("Salvataggio",
                f"Impostazioni salvate in:\n{self._cfg_path}")
        else:
            messagebox.showerror("Errore",
                f"Impossibile scrivere {self._cfg_path}")

    def _browse_sql(self):
        init = resolve_sql_path(self._pv_sql_path.get())
        fp = filedialog.asksaveasfilename(
            title="Seleziona/crea file SQLite",
            defaultextension=".sqlite",
            initialfile=os.path.basename(init),
            initialdir=os.path.dirname(init) or get_app_dir(),
            filetypes=[("SQLite", "*.sqlite *.db3 *.sqlite3"), ("Tutti", "*.*")])
        if fp:
            # Se è dentro cartella app, salva relativo
            app_dir = get_app_dir()
            try:
                rel = os.path.relpath(fp, app_dir)
                if not rel.startswith('..'):
                    self._pv_sql_path.set(rel)
                else:
                    self._pv_sql_path.set(fp)
            except ValueError:
                self._pv_sql_path.set(fp)

    # ══════════════════════════════════════════════════════════
    #  HELPERS
    # ══════════════════════════════════════════════════════════
    def app_log(self, msg, tag="info"):
        self._lbl_status.config(text=msg)

    def _open_file(self):
        fp = filedialog.askopenfilename(
            title="Apri file .db TIA Portal",
            initialdir=get_app_dir(),
            filetypes=[("TIA Portal DB", "*.db"), ("Tutti i file", "*.*")])
        if not fp: return
        try:
            data = parse_db_file(fp)
            self._load_data(data)
        except Exception as e:
            messagebox.showerror("Errore parser", str(e))

    def _save_plot(self, fig):
        fp = filedialog.asksaveasfilename(
            title="Salva grafico come PNG",
            defaultextension=".png",
            initialdir=get_app_dir(),
            filetypes=[("PNG", "*.png"), ("SVG", "*.svg"), ("PDF", "*.pdf")])
        if not fp: return
        try:
            fig.savefig(fp, dpi=150, facecolor=DARK_BG, bbox_inches='tight')
            self.app_log(f"Grafico salvato: {fp}")
        except Exception as e:
            messagebox.showerror("Errore", str(e))

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

    # ─── Caricamento dati in UI ────────────────────────────────
    def _load_data(self, data):
        self.db_data = data
        fn = data.get('filename', '—')
        self._lbl_file.config(text=fn)
        self._update_results_panel()
        self._draw_all()
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
            self._lbl_verdict.config(fg=WARN_CLR)
        elif nok:
            self._pv_verdict.set("✗ DOPPIO SPESSORE")
            self._lbl_verdict.config(fg=ERR_CLR)
        else:
            self._pv_verdict.set("✓ OK")
            self._lbl_verdict.config(fg=OK_CLR)

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
        pos = sc.get('I_ActPosition', None)
        self._pv_pos.set(f"{pos:.2f}" if pos is not None else "—")
        speed = sc.get('I_ActSpeed', None)
        self._pv_speed.set(f"{speed:.2f}" if speed is not None else "—")


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = ThicknessApp()
    app.mainloop()
