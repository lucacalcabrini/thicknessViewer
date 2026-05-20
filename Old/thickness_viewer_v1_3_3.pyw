# -*- coding: utf-8 -*-
"""
ThicknessProfiler DB Analyzer  v1.1.0
Allineato a: Fb936_ControlloSpessore_v0  VERSION 1.1

Fisica del sistema:
  Laser SOTTO il piano di trasferimento, ventosa sopra.
  La quota letta diminuisce all'aumentare dello spessore del disco.
  aBaseline[i] = laser_cal + SpessoreDiscoRiferimento = QUOTA SUPPORTO
  aProfiloSpessore[i] = aBaseline[i] - laser_misurato = spessore reale disco
  aProfiloDelta[i]    = aProfiloSpessore[i] - SpessoreDiscoRiferimento = eccesso

NOK (fix v1.1):
  Il confronto avviene su aProfiloDelta (eccesso), NON su aProfiloSpessore (assoluto).
  SpessoreMassimo = max delta tollerato [mm].

Requisiti: pip install matplotlib numpy
Opzionale: pip install python-snap7  (PLC Reader / Auto-Export)
Build EXE: pyinstaller --onefile --windowed thickness_viewer_v1_1_0.pyw
"""

APP_VERSION = "1.3.3"
APP_BUILD   = "2026-04-28"
APP_RELEASE = f"v{APP_VERSION} build {APP_BUILD}"
FB_TARGET   = "Fb936_ControlloSpessore_v12"
FB_SCL_NAME = '"Fb936_ControlloSpessore_v12"'

import sys
if sys.platform == "win32":
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd: ctypes.windll.user32.ShowWindow(hwnd, 0)
        ctypes.windll.kernel32.FreeConsole()
    except Exception: pass

import os, signal, atexit
_MAIN_PID = os.getpid()

def _kill_tree():
    if os.getpid() != _MAIN_PID: return
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.call(["taskkill","/F","/T","/PID",str(_MAIN_PID)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)
        else:
            os.killpg(os.getpgid(_MAIN_PID), signal.SIGKILL)
    except Exception:
        try: os.kill(_MAIN_PID, signal.SIGKILL)
        except Exception: pass

atexit.register(_kill_tree)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re, datetime, time, struct, sqlite3, configparser

import matplotlib; matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import numpy as np

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
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = "thickness_viewer.ini"

def load_settings():
    path = os.path.join(get_app_dir(), SETTINGS_FILE)
    cfg = configparser.ConfigParser()
    cfg['PLC'] = {'ip':'192.168.0.1','rack':'0','slot':'1','db':'16070'}
    cfg['SQL'] = {'path':'thickness_archive.sqlite'}
    if os.path.isfile(path):
        try: cfg.read(path, encoding='utf-8')
        except Exception: pass
    return cfg, path

def save_settings(cfg, path):
    try:
        with open(path, 'w', encoding='utf-8') as f: cfg.write(f)
        return True
    except Exception: return False

def resolve_sql(raw):
    return raw if os.path.isabs(raw) else os.path.join(get_app_dir(), raw)


# ══════════════════════════════════════════════════════════════════
#  PARSER FILE .DB TIA PORTAL
# ══════════════════════════════════════════════════════════════════

_RE_ARR  = re.compile(r'([\w.]+)\[(\d+)\]\s*:=\s*([+-]?[\d]*\.?[\d]+(?:[eE][+-]?\d+)?)\s*;')
_RE_SCL  = re.compile(r'^[ \t]*([\w.]+)\s*:=\s*([^;]+?)\s*;', re.MULTILINE)
_RE_BEG  = re.compile(r'\bBEGIN\b', re.IGNORECASE)

def _parse(text, result):
    m = _RE_BEG.search(text)
    if not m: raise ValueError("Blocco BEGIN non trovato")
    body = text[m.end():]
    raw = {}
    for m2 in _RE_ARR.finditer(body):
        n, i, v = m2.group(1), int(m2.group(2)), float(m2.group(3))
        raw.setdefault(n, {})[i] = v
    for name, d in raw.items():
        result["arrays"][name] = [d.get(i, float("nan")) for i in range(max(d)+1)]
    an = set(raw)
    for m2 in _RE_SCL.finditer(body):
        name, rv = m2.group(1), m2.group(2).strip()
        if name in an or ('[' in name and ']' in name): continue
        u = rv.upper()
        try:
            result["scalars"][name] = (True if u=="TRUE" else False if u=="FALSE"
                                       else float(rv))
        except ValueError:
            result["scalars"][name] = rv
    return result

def parse_db_file(fp):
    with open(fp, "r", encoding="utf-8", errors="replace") as f: text=f.read()
    r={"scalars":{},"arrays":{},"raw_text":text,"filename":os.path.basename(fp),
       "filepath":fp,"loaded_at":datetime.datetime.now()}
    return _parse(text, r)

def parse_db_text(text, filename="PLC_direct.db"):
    r={"scalars":{},"arrays":{},"raw_text":text,"filename":filename,
       "loaded_at":datetime.datetime.now()}
    return _parse(text, r)


# ══════════════════════════════════════════════════════════════════
#  OFFSET MAP — Fb936_ControlloSpessore_v0 v1.1 (S7_Optimized=FALSE)
#
#  Rispetto a v1.2: aggiunto AppNcelleFuoriSoglia : Int @ 4992
#  Offset successivi spostati di +2: AppValidValue→4994, OldDirLavoro→4996,
#  AuxFpCambioDirezione→4998. DB totale: 5000 byte (era 4998).
#
#  Questa mappa è valida con S7_Optimized_Access := 'FALSE' (Db16070). ✓
#  Questa mappa è valida SOLO se si porta entrambi a 'FALSE'.
#
#  UDT "UdtControlloSpessore" (36 byte):
#   0  PosizioneCentroVentosa     Real(4)
#   4  RangeControllo             Real(4)   default 60→80mm
#   8  RangeRallenta              Real(4)
#  12  nLettureConsecutiveAllarme Real(4)
#  16  SpessoreMassimo            Real(4)   MAX DELTA su aProfiloDelta [mm]
#  20  OvrTrasfertPerpassaggio    LReal(8)
#  28  DisabilitaControllo        Bool(28.0)
#  30  SpessoreDiscoRiferimento   Real(4)   [mm] disco campione
#  34  AbilitaTaratura            Bool(34.0)
# ══════════════════════════════════════════════════════════════════

ARRAY_SIZE = 201    # Array[0..200], MaxData=200
PLC_R  = 4
PLC_LR = 8
PLC_I  = 2


class _OB:
    def __init__(self):
        self.e = []; self.off = 0

    def _a(self, n=2):
        if self.off % n: self.off += n - self.off % n

    def real(self, name):
        self._a(); self.e.append((name,self.off,'real',PLC_R)); self.off+=PLC_R

    def lreal(self, name):
        self._a(); self.e.append((name,self.off,'lreal',PLC_LR)); self.off+=PLC_LR

    def int16(self, name):
        self._a(); self.e.append((name,self.off,'int',PLC_I)); self.off+=PLC_I

    def bools(self, names):
        self._a(); base=self.off
        for i,n in enumerate(names):
            self.e.append((n,base+i//8,'bool',i%8))
        self.off = base + (len(names)-1)//8 + 1

    def arr_r(self, name, n):
        self._a(); self.e.append((name,self.off,'array_real',n)); self.off+=n*PLC_R

    def arr_i(self, name, n):
        self._a(); self.e.append((name,self.off,'array_int',n)); self.off+=n*PLC_I

    def skip(self, n): self._a(); self.off+=n


def build_offset_map():
    """Offset map per Fb936_ControlloSpessore_v12 v1.2
    Verificata empiricamente: v1.1 DB=4992b, VAR_IN_OUT=2b cad.
    v1.2 aggiunge I_SpessoreAtteso:Real(4) → totale 4996b."""
    b = _OB()
    # ── UDT VAR_INPUT RETAIN (36 byte) ──────────────────────────────
    b.real('I_ParametriCntrolloSpessore.PosizioneCentroVentosa')    # 0
    b.real('I_ParametriCntrolloSpessore.RangeControllo')             # 4
    b.real('I_ParametriCntrolloSpessore.RangeRallenta')              # 8
    b.real('I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme') # 12
    b.real('I_ParametriCntrolloSpessore.SpessoreMassimo')            # 16
    b.lreal('I_ParametriCntrolloSpessore.OvrTrasfertPerpassaggio')   # 20
    b.bools(['I_ParametriCntrolloSpessore.DisabilitaControllo'])      # 28.0
    b._a(); b.real('I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento')  # 30
    b.bools(['I_ParametriCntrolloSpessore.AbilitaTaratura'])          # 34.0
    b._a()                                                             # → 36
    # ── VAR_INPUT ─────────────────────────────────────────────────
    b.real('I_Spessore_mm')                                           # 36
    b.bools(['I_InvertiLettura','I_PiecePresence'])                    # 40.0,40.1
    b._a()                                                             # → 42
    b.lreal('I_ActPosition')                                           # 42
    b.lreal('I_ActSpeed')                                              # 50
    b.int16('I_DirLavoro')                                             # 58
    b.real('I_MaxRangeLaser')                                          # 60
    b.real('I_MinRangeLaser')                                          # 64
    b.real('I_SpessoreAtteso')                                         # 68 ← v1.2
    # ── VAR_OUTPUT ────────────────────────────────────────────────
    b.bools(['O_SpessoreOk','O_SpessoreNOK','O_AlmControlloDisattivo',
             'O_TaraturaAttiva','O_TaraturaCompletata'])                # 72.x
    b._a()                                                             # → 74
    b.real('O_SpessoreMedio')                                          # 74
    b.real('O_SpessoreMax')                                            # 78
    b.real('O_DeltaMedio')                                             # 82
    b.real('O_DeltaMax')                                               # 86
    b.int16('O_nCelleProfilo')                                         # 90
    b.int16('O_nCelleFuoriSoglia')                                     # 92
    b.bools(['O_LaserInRange'])                                        # 94.0
    b._a()                                                             # → 96
    # ── VAR_IN_OUT (2 byte cad. su S7-1500 non-ottimizzato) ─────────
    b.skip(2)  # IO_OvrAuto                                            # 96
    b.skip(2)  # IO_OvrMan                                             # 98 → 100
    # ── VAR Pinza ─────────────────────────────────────────────────
    b.bools(['Pinza.InZonaControllo','Pinza.InZonaRallenta'])          # 100.x
    b._a()                                                             # → 102
    b.lreal('Pinza.OvrAutoOld')                                        # 102
    b.lreal('Pinza.OvrManOld')                                         # 110
    b.real('Pinza.nLettureConsecutive')                                # 118 → 122
    # ── VAR RETAIN: baseline ──────────────────────────────────────
    b.arr_r('aBaseline', ARRAY_SIZE)                                   # 122 → 926
    b.bools(['baselineValida'])                                         # 926.0
    b._a()                                                             # → 928
    # ── VAR: R_TRIG/F_TRIG (2 byte cad.) ─────────────────────────
    b.skip(2)  # Fp                                                    # 928
    b.skip(2)  # Fn                                                    # 930
    b.skip(2)  # FpSlowing                                            # 932
    b.skip(2)  # FnSlowing                                            # 934
    b.skip(2)  # FpTaratura                                           # 936
    b.bools(['DirOk','Ripeti'])                                        # 938.x
    b._a()                                                             # → 940
    b.int16('AppMinIndexSurce')                                        # 940
    b.int16('AppMaxIndexSurce')                                        # 942
    b.int16('AppMaxIndex')                                             # 944
    b.bools(['taraturaInCorso'])                                       # 946.0
    b._a()                                                             # → 948
    # ── VAR: buffer profilometro ───────────────────────────────────
    b.arr_r('aSomRaw',  ARRAY_SIZE)                                    # 948  → 1752
    b.arr_i('aNraw',    ARRAY_SIZE)                                    # 1752 → 2154
    b.arr_r('aSomCal',  ARRAY_SIZE)                                    # 2154 → 2958
    b.arr_i('aNcal',    ARRAY_SIZE)                                    # 2958 → 3360
    b.arr_r('aProfiloSpessore', ARRAY_SIZE)                            # 3360 → 4164
    b.arr_r('aProfiloDelta',    ARRAY_SIZE)                            # 4164 → 4968
    # ── VAR: scalari risultati ────────────────────────────────────
    b.bools(['AppSpessoreOk','AppSpessoreNok'])                        # 4968.x
    b._a()                                                             # → 4970
    b.real('AppSpessoreMedio')                                         # 4970
    b.real('AppSpessoreMax')                                           # 4974
    b.real('AppDeltaMedio')                                            # 4978
    b.real('AppDeltaMax')                                              # 4982
    b.int16('AppNcelleProfilo')                                        # 4986
    b.int16('AppNcelleFuoriSoglia')                                    # 4988
    b.bools(['AppValidValue'])                                         # 4990.0
    b._a()                                                             # → 4992
    b.int16('OldDirLavoro')                                            # 4992
    b.bools(['AuxFpCambioDirezione'])                                  # 4994.0
    b._a()                                                             # → 4996
    b.e.append(('IdLettura', 4996, 'dint', 4))                        # 4996 DInt ← trigger
    # DB totale: 5000 byte
    return b.e, 5000


# ── Decode primitives ─────────────────────────────────────────────
def _r(d,o):  return struct.unpack('>f',d[o:o+4])[0]
def _lr(d,o): return struct.unpack('>d',d[o:o+8])[0]
def _i(d,o):  return struct.unpack('>h',d[o:o+2])[0]
def _di(d,o): return struct.unpack('>i',d[o:o+4])[0]   # DInt
def _b(d,o,b):return bool(d[o]&(1<<b))
def _ar(d,o,n): return list(struct.unpack(f'>{n}f',d[o:o+n*4]))
def _ai(d,o,n): return list(struct.unpack(f'>{n}h',d[o:o+n*2]))

def decode_db(raw, omap):
    r={'scalars':{},'arrays':{}}
    for name,off,dtype,sz in omap:
        try:
            if   dtype=='real':       r['scalars'][name]=_r(raw,off)
            elif dtype=='lreal':      r['scalars'][name]=_lr(raw,off)
            elif dtype=='int':        r['scalars'][name]=_i(raw,off)
            elif dtype=='dint':       r['scalars'][name]=_di(raw,off)
            elif dtype=='bool':       r['scalars'][name]=_b(raw,off,sz)
            elif dtype=='array_real': r['arrays'][name]=_ar(raw,off,sz)
            elif dtype=='array_int':  r['arrays'][name]=_ai(raw,off,sz)
        except (struct.error, IndexError):
            r[('arrays' if 'array' in dtype else 'scalars')][name]=(
                [0.0]*sz if 'array' in dtype else
                False if dtype=='bool' else 0)
    return r

def gen_db_text(decoded, db_name="Thickness_Export"):
    lines=[f'DATA_BLOCK "{db_name}"',
           "{ S7_Optimized_Access := 'FALSE' }",
           "VERSION : 0.2",
           "NON_RETAIN",
           FB_SCL_NAME,"BEGIN"]
    for k,v in decoded['scalars'].items():
        if isinstance(v,bool): lines.append(f"   {k} := {'TRUE' if v else 'FALSE'};")
        elif isinstance(v,int): lines.append(f"   {k} := {v};")
        else: lines.append(f"   {k} := {v:.7e};")
    for k,arr in decoded['arrays'].items():
        for i,v in enumerate(arr): lines.append(f"   {k}[{i}] := {v:.7e};")
    lines.append("END_DATA_BLOCK")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  PLC READER
# ══════════════════════════════════════════════════════════════════

class PLCReader:
    def __init__(self, ip, rack=0, slot=1):
        if not SNAP7_AVAILABLE:
            raise ImportError("pip install python-snap7")
        self.ip=ip; self.rack=rack; self.slot=slot
        self.client=snap7.client.Client()

    def connect(self):
        self.client.connect(self.ip,self.rack,self.slot)
        if not self.client.get_connected():
            raise ConnectionError(f"Connessione fallita: {self.ip}")
        info=self.client.get_cpu_info()
        return info.ModuleTypeName.decode().strip(), self.client.get_pdu_length()

    def disconnect(self):
        if self.client.get_connected(): self.client.disconnect()

    def detect_db_size(self, db_num, hint=8000):
        """Ricerca binaria per trovare la dimensione reale del DB.
        Restituisce il numero di byte effettivamente leggibili."""
        # Verifica che il DB esista
        try:
            self.client.db_read(db_num, 0, 1)
        except Exception:
            return 0
        lo, hi = 1, hint
        while lo < hi:
            mid = (lo + hi + 1) // 2
            try:
                self.client.db_read(db_num, mid - 1, 1)
                lo = mid
            except Exception:
                hi = mid - 1
        return lo

    def read_counter(self, db_num, offset=4996):
        """Legge solo il DInt IdLettura (4 byte). Velocissimo, usato per il poll."""
        try:
            raw = self.client.db_read(db_num, offset, 4)
            return _di(raw, 0)
        except Exception:
            return None

    def read_db_raw(self, db_num, total, chunk=400, actual_size=None):
        """Legge raw bytes dal DB.
        Se actual_size < total, le aree oltre actual_size restano a zero
        (decodificate come 0 / False / 0.0 → comportamento sicuro).
        """
        limit = actual_size if actual_size and actual_size < total else total
        data = bytearray(total)   # zero-filled: offset oltre actual_size = valore neutro
        off = 0
        while off < limit:
            sz = min(chunk, limit - off)
            try:
                data[off:off+sz] = self.client.db_read(db_num, off, sz)
            except Exception as e:
                err = str(e).lower()
                if 'out of range' in err or 'address' in err:
                    break
                raise RuntimeError(
                    f"DB{db_num} @{off}: {e}\n"
                    "Verifica: 1) DB esiste  "
                    "2) S7_Optimized_Access=FALSE  "
                    "3) PUT/GET abilitato")
            off += sz
        return data


# ══════════════════════════════════════════════════════════════════
#  SQLITE
# ══════════════════════════════════════════════════════════════════

def sql_init(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True)
    con=sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS acquisizioni (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, db_number INTEGER,
        spessore_medio REAL, spessore_max REAL,
        delta_medio REAL, delta_max REAL,
        n_celle INTEGER, n_celle_fuori INTEGER,
        spessore_ok INTEGER, spessore_nok INTEGER,
        taratura INTEGER, raw_db TEXT)""")
    for col in ['n_celle_fuori']:
        try: con.execute(f"ALTER TABLE acquisizioni ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON acquisizioni(timestamp DESC)")
    con.commit(); return con

def sql_insert(con, dec, db_num=0, is_tar=False):
    sc=dec['scalars']
    ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    cur=con.execute("""INSERT INTO acquisizioni
        (timestamp,db_number,spessore_medio,spessore_max,delta_medio,delta_max,
         n_celle,n_celle_fuori,spessore_ok,spessore_nok,taratura,raw_db)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(
        ts,db_num,
        float(sc.get('AppSpessoreMedio',sc.get('O_SpessoreMedio',0))),
        float(sc.get('AppSpessoreMax',sc.get('O_SpessoreMax',0))),
        float(sc.get('AppDeltaMedio',sc.get('O_DeltaMedio',0))),
        float(sc.get('AppDeltaMax',sc.get('O_DeltaMax',0))),
        int(sc.get('AppNcelleProfilo',sc.get('O_nCelleProfilo',0))),
        int(sc.get('AppNcelleFuoriSoglia',sc.get('O_nCelleFuoriSoglia',0))),
        int(bool(sc.get('AppSpessoreOk',sc.get('O_SpessoreOk',False)))),
        int(bool(sc.get('AppSpessoreNok',sc.get('O_SpessoreNOK',False)))),
        int(bool(is_tar)), gen_db_text(dec,f"DB{db_num}_{ts}")))
    con.commit(); return cur.lastrowid

def sql_query(con,limit=500,filtro=None):
    q=("SELECT id,timestamp,db_number,spessore_medio,spessore_max,"
       "delta_medio,delta_max,n_celle,n_celle_fuori,spessore_ok,spessore_nok,taratura "
       "FROM acquisizioni")
    if filtro=='ok':    q+=" WHERE spessore_ok=1 AND spessore_nok=0 AND taratura=0"
    elif filtro=='nok': q+=" WHERE spessore_nok=1"
    elif filtro=='tar': q+=" WHERE taratura=1"
    return list(con.execute(q+f" ORDER BY timestamp DESC LIMIT {limit}"))

def sql_raw(con,rid):
    r=con.execute("SELECT raw_db FROM acquisizioni WHERE id=?",(rid,)).fetchone()
    return r[0] if r else None

def sql_count(con):
    r=con.execute("SELECT COUNT(*) FROM acquisizioni").fetchone()
    return int(r[0]) if r else 0

def sql_delete(con,rid):
    con.execute("DELETE FROM acquisizioni WHERE id=?",(rid,)); con.commit()


# ══════════════════════════════════════════════════════════════════
#  PALETTE
# ══════════════════════════════════════════════════════════════════
DARK_BG="#000000";  PANEL_BG="#0d1117";  BORDER_CLR="#484f58"
ACCENT="#79c0ff";   OK_CLR="#56d364";    WARN_CLR="#e3b341"
ERR_CLR="#FF6B6B";  TEXT_CLR="#f0f6fc"; MUTED_CLR="#b1bac4"
ENTRY_BG="#161b22"
PROFILE_CLR="#79c0ff";   BASELINE_CLR="#e3b341"; DELTA_CLR="#ff9070"
NOK_LINE_CLR="#ff6e85";  FUORI_CLR="#ff4d6d";    PLC_CLR="#f0883e"
AUTOEXP_CLR="#56d364";   TAR_CLR="#d2a8ff";       LASER_CLR="#4fc3f7"
WARN_BG="#7d2a00"   # (colore riservato per futuri banner)


# ══════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════

class ThicknessApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"◈ Thickness Profiler  {APP_RELEASE}  —  {FB_TARGET}")
        self.geometry("1460x940"); self.minsize(1150,730)
        self.configure(bg=DARK_BG)

        self._cfg, self._cfg_path = load_settings()
        self.db_data = None
        self._ae_running = False
        self._ae_timer   = None
        self._ae_client  = None
        self._ae_cok = self._ae_cnok = self._ae_ctar = 0
        self._ae_sql_con = None
        self._ae_prev    = None
        # Multi-DB: lista di dict con {enabled, db_num, label, prev_counter}
        # Costruita in _tab_autoexp, modificabile a runtime
        self._ae_db_slots = []
        self._plc_client = None
        self._plc_dec    = None

        self._omap, self._db_size = build_offset_map()
        self._actual_db_size = self._db_size   # aggiornato dopo detect_db_size()

        self._style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._startup)

    # ── Settings ─────────────────────────────────────────────
    def _save_ini(self):
        try:
            ip   = getattr(self,'_pv_ip',   tk.StringVar(value=self._cfg['PLC'].get('ip','192.168.0.1'))).get().strip()
            rack = getattr(self,'_pv_rack', tk.StringVar(value='0')).get()
            slot = getattr(self,'_pv_slot', tk.StringVar(value='1')).get()
            db   = getattr(self,'_pv_db',   tk.StringVar(value='16010')).get()
            sql  = getattr(self,'_pv_sql',  tk.StringVar(value='thickness_archive.sqlite')).get()
            self._cfg['PLC'] = {'ip':ip,'rack':rack,'slot':slot,'db':db}
            self._cfg['SQL'] = {'path':sql}
            save_settings(self._cfg, self._cfg_path)
        except Exception: pass

    def _startup(self):
        miss=[m for m in ['numpy','matplotlib']
              if not __import__('importlib').util.find_spec(m)]
        if miss:
            messagebox.showwarning("Librerie mancanti",
                f"pip install {' '.join(miss)}")
        if not os.path.isfile(self._cfg_path):
            self._save_ini()

    def _on_close(self):
        if self._ae_running: self._ae_stop()
        self._save_ini()
        try: self.destroy()
        except Exception: pass
        _kill_tree()

    # ── Stile ─────────────────────────────────────────────────
    def _style(self):
        st=ttk.Style(self); st.theme_use("clam")
        base=dict(background=DARK_BG,foreground=TEXT_CLR,fieldbackground=ENTRY_BG,
                  troughcolor=PANEL_BG,bordercolor=BORDER_CLR,
                  lightcolor=BORDER_CLR,darkcolor=BORDER_CLR,font=("Consolas",10))
        st.configure(".",**base)
        for w in ["TFrame","TLabel"]:
            st.configure(w,background=DARK_BG,foreground=TEXT_CLR)
        st.configure("Muted.TLabel",background=DARK_BG,foreground=MUTED_CLR,font=("Consolas",9))
        st.configure("Title.TLabel",background=DARK_BG,foreground=ACCENT,font=("Consolas",12,"bold"))
        for n,bg,fg in [("Accent",ACCENT,DARK_BG),("Plc",PLC_CLR,DARK_BG),
                         ("Auto",AUTOEXP_CLR,DARK_BG)]:
            st.configure(f"{n}.TButton",background=bg,foreground=fg,
                         font=("Consolas",10,"bold"),padding=(10,5))
        st.configure("TButton",background=ENTRY_BG,foreground=TEXT_CLR,
                     bordercolor=BORDER_CLR,padding=(8,4))
        st.map("TButton",background=[("active",ACCENT)],foreground=[("active",DARK_BG)])
        st.configure("TNotebook",background=DARK_BG,bordercolor=BORDER_CLR)
        st.configure("TNotebook.Tab",background=PANEL_BG,foreground=MUTED_CLR,
                     padding=(14,5),bordercolor=BORDER_CLR)
        st.map("TNotebook.Tab",background=[("selected",DARK_BG)],
               foreground=[("selected",ACCENT)])
        st.configure("TEntry",fieldbackground=ENTRY_BG,foreground=TEXT_CLR,insertcolor=TEXT_CLR)
        st.configure("TLabelframe",background=DARK_BG,foreground=MUTED_CLR,bordercolor=BORDER_CLR)
        st.configure("TLabelframe.Label",background=DARK_BG,foreground=MUTED_CLR)
        st.configure("Treeview",background=ENTRY_BG,foreground=TEXT_CLR,
                     fieldbackground=ENTRY_BG,rowheight=22,font=("Consolas",9))
        st.configure("Treeview.Heading",background=PANEL_BG,foreground=ACCENT,
                     relief="flat",font=("Consolas",9,"bold"))
        st.map("Treeview",background=[("selected",ACCENT)],foreground=[("selected",DARK_BG)])
        st.configure("TCheckbutton",background=DARK_BG,foreground=TEXT_CLR,
                     indicatorcolor="#1f6feb",indicatorrelief="flat")
        st.configure("TScrollbar",background=PANEL_BG,troughcolor=DARK_BG,
                     arrowcolor=MUTED_CLR,bordercolor=BORDER_CLR)
        st.configure("TCombobox",fieldbackground=ENTRY_BG,background=ENTRY_BG,
                     foreground=TEXT_CLR,arrowcolor=TEXT_CLR,bordercolor=BORDER_CLR)
        st.map("TCombobox",
               fieldbackground=[("readonly",ENTRY_BG)],foreground=[("readonly",TEXT_CLR)])

    # ── Layout principale ─────────────────────────────────────
    def _build_ui(self):
        top=ttk.Frame(self); top.pack(fill="x",padx=10,pady=(8,0))
        ttk.Label(top,text="◈ THICKNESS PROFILER",style="Title.TLabel").pack(side="left")
        tk.Label(top,text=APP_RELEASE,font=("Consolas",9),fg="#58a6ff",bg=DARK_BG,padx=8).pack(side="left")
        tk.Label(top,text=FB_TARGET,font=("Consolas",9),fg=MUTED_CLR,bg=DARK_BG,padx=8).pack(side="left")

        # Badge stato Auto-Export (sempre visibile)
        self._lbl_ae_badge=tk.Label(top,text="⚡ AUTO-EXPORT: OFF",
            font=("Consolas",9,"bold"),fg=MUTED_CLR,bg=DARK_BG,padx=8)
        self._lbl_ae_badge.pack(side="left")
        ttk.Button(top,text="📁 Apri file .db...",style="Accent.TButton",
                   command=self._open_file).pack(side="right",padx=2)

        main=ttk.PanedWindow(self,orient="horizontal")
        main.pack(fill="both",expand=True,padx=10,pady=6)

        self._left=ttk.Frame(main,width=370); self._left.pack_propagate(False)
        main.add(self._left,weight=0)
        rp=ttk.Frame(main); main.add(rp,weight=1)
        self._build_left(self._left)
        self._build_right(rp)

        bot=ttk.Frame(self); bot.pack(fill="x",padx=10,pady=(0,4))
        self._lbl_st=tk.Label(bot,text="Pronto.",bg=DARK_BG,fg=MUTED_CLR,
                               font=("Consolas",9),anchor="w")
        self._lbl_st.pack(side="left",fill="x",expand=True)
        self._lbl_file=tk.Label(bot,text="Nessun dato",bg=DARK_BG,fg=ACCENT,
                                  font=("Consolas",9,"bold"),anchor="e")
        self._lbl_file.pack(side="right")

    # ── Pannello sinistro ──────────────────────────────────────
    def _build_left(self, P):
        cv=tk.Canvas(P,bg=DARK_BG,highlightthickness=0)
        sb=ttk.Scrollbar(P,orient="vertical",command=cv.yview)
        inn=ttk.Frame(cv)
        inn.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        wid=cv.create_window((0,0),window=inn,anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.bind("<Configure>",lambda e:cv.itemconfigure(wid,width=e.width))
        cv.bind("<MouseWheel>",lambda e:cv.yview_scroll(int(-1*(e.delta/120)),"units"))
        sb.pack(side="right",fill="y"); cv.pack(side="left",fill="both",expand=True)

        # RISULTATO
        br=ttk.LabelFrame(inn,text="  RISULTATO ULTIMA PASSATA  ",padding=8)
        br.pack(fill="x",padx=4,pady=4)
        self._pv_verd=tk.StringVar(value="—")
        self._lbl_verd=tk.Label(br,textvariable=self._pv_verd,bg=DARK_BG,
                                  font=("Consolas",18,"bold"),fg=MUTED_CLR)
        self._lbl_verd.pack(pady=4)
        self._pv_fuori=tk.StringVar(value="Celle fuori soglia: —")
        self._lbl_fuori=tk.Label(br,textvariable=self._pv_fuori,bg=DARK_BG,
                                   font=("Consolas",11,"bold"),fg=MUTED_CLR)
        self._lbl_fuori.pack(pady=(0,4))

        # Scalari
        g=ttk.Frame(br); g.pack(fill="x",pady=2)
        self._pvs={}
        for r,(lbl,k) in enumerate([
                ("Spessore medio","SpessoreMedio"),("Spessore max","SpessoreMax"),
                ("Delta medio","DeltaMedio"),("Delta max","DeltaMax"),
                ("Celle campionate","nCelleProfilo")]):
            tk.Label(g,text=lbl,bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",9),
                     width=18,anchor="w").grid(row=r,column=0,sticky="w",padx=2,pady=1)
            sv=tk.StringVar(value="—")
            tk.Label(g,textvariable=sv,bg=DARK_BG,fg=ACCENT,
                     font=("Consolas",10,"bold"),width=12,anchor="e"
                     ).grid(row=r,column=1,sticky="e",padx=2,pady=1)
            self._pvs[k]=sv

        # TARATURA
        bt=ttk.LabelFrame(inn,text="  TARATURA  ",padding=8)
        bt.pack(fill="x",padx=4,pady=4)
        self._pv_tar=tk.StringVar(value="—")
        tk.Label(bt,textvariable=self._pv_tar,bg=DARK_BG,
                 font=("Consolas",11,"bold"),fg=MUTED_CLR).pack(pady=2)
        self._pv_tar_rif=tk.StringVar(value="Riferimento: — mm")
        tk.Label(bt,textvariable=self._pv_tar_rif,bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",9)).pack()

        # PARAMETRI UDT
        bp=ttk.LabelFrame(inn,text="  PARAMETRI UDT  ",padding=6)
        bp.pack(fill="x",padx=4,pady=4)
        self._pv_par={}
        for k,lbl in [
                ('PosizioneCentroVentosa','Centro ventosa'),
                ('RangeControllo','Range controllo'),
                ('RangeRallenta','Range rallenta'),
                ('SpessoreMassimo','Tolleranza NOK [mm]'),
                ('nLettureConsecutiveAllarme','N. celle consec.'),
                ('OvrTrasfertPerpassaggio','Override %'),
                ('SpessoreDiscoRiferimento','Disco taratura [mm]'),
                ('DisabilitaControllo','Disabilitato'),
                ('AbilitaTaratura','Tarat. abilitata')]:
            row=ttk.Frame(bp); row.pack(fill="x",pady=1)
            tk.Label(row,text=lbl,bg=DARK_BG,fg=MUTED_CLR,
                     font=("Consolas",9),width=18,anchor="w").pack(side="left")
            sv=tk.StringVar(value="—")
            tk.Label(row,textvariable=sv,bg=DARK_BG,fg=TEXT_CLR,
                     font=("Consolas",9),anchor="e").pack(side="right")
            self._pv_par[k]=sv

        # SPESSORE ATTESO PRODUZIONE (v1.2)
        ba=ttk.LabelFrame(inn,text="  SPESSORE ATTESO PRODUZIONE  ",padding=6)
        ba.pack(fill="x",padx=4,pady=4)
        self._pv_sp_att=tk.StringVar(value="—")
        self._lbl_sp_att=tk.Label(ba,textvariable=self._pv_sp_att,
                                   bg=DARK_BG,font=("Consolas",16,"bold"),fg=ACCENT)
        self._lbl_sp_att.pack(pady=2)
        tk.Label(ba,text="NOK se spessore > atteso + tolleranza",
                 bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",8)).pack()

        # LASER RANGE
        bl=ttk.LabelFrame(inn,text="  LASER RANGE (grezzo)  ",padding=6)
        bl.pack(fill="x",padx=4,pady=4)
        self._pv_lmin=tk.StringVar(value="—"); self._pv_lmax=tk.StringVar(value="—")
        self._pv_lcur=tk.StringVar(value="—"); self._pv_lval=tk.StringVar(value="—")
        for lbl,var,c in [("Min valido:",self._pv_lmin,LASER_CLR),
                           ("Max valido:",self._pv_lmax,LASER_CLR),
                           ("Lettura attuale:",self._pv_lcur,LASER_CLR)]:
            row=ttk.Frame(bl); row.pack(fill="x",pady=1)
            tk.Label(row,text=lbl,bg=DARK_BG,fg=MUTED_CLR,
                     font=("Consolas",9),width=18,anchor="w").pack(side="left")
            tk.Label(row,textvariable=var,bg=DARK_BG,fg=c,
                     font=("Consolas",9),anchor="e").pack(side="right")
        row=ttk.Frame(bl); row.pack(fill="x",pady=2)
        tk.Label(row,text="Valido:",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",9),width=18,anchor="w").pack(side="left")
        self._lbl_lval=tk.Label(row,textvariable=self._pv_lval,bg=DARK_BG,
                                  fg=MUTED_CLR,font=("Consolas",10,"bold"),anchor="e")
        self._lbl_lval.pack(side="right")

        # STATO PINZA
        bz=ttk.LabelFrame(inn,text="  STATO PINZA  ",padding=6)
        bz.pack(fill="x",padx=4,pady=4)
        self._pv_piece=tk.StringVar(value="—"); self._pv_zc=tk.StringVar(value="—")
        self._pv_zr=tk.StringVar(value="—"); self._pv_pos=tk.StringVar(value="—")
        self._pv_spd=tk.StringVar(value="—"); self._pv_dir=tk.StringVar(value="—")
        for lbl,var in [("Pezzo presente:",self._pv_piece),("In zona ctrl:",self._pv_zc),
                         ("In zona rall:",self._pv_zr),("Posizione [mm]:",self._pv_pos),
                         ("Velocità:",self._pv_spd),("DirLavoro:",self._pv_dir)]:
            row=ttk.Frame(bz); row.pack(fill="x",pady=1)
            tk.Label(row,text=lbl,bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",9),
                     width=16,anchor="w").pack(side="left")
            tk.Label(row,textvariable=var,bg=DARK_BG,fg=TEXT_CLR,
                     font=("Consolas",9,"bold"),anchor="e").pack(side="right")

    # ── Pannello destro ───────────────────────────────────────
    def _build_right(self, P):
        self._nb=ttk.Notebook(P); self._nb.pack(fill="both",expand=True)
        for title,builder in [
                ("  📊 Profilo  ",  self._tab_profilo),
                ("  📈 Delta  ",    self._tab_delta),
                ("  🔌 PLC Reader  ",self._tab_plc),
                ("  ⚡ Auto-Export  ",self._tab_autoexp),
                ("  📚 History  ",  self._tab_history),
                ("  ⚙ Impostazioni  ",self._tab_settings)]:
            t=ttk.Frame(self._nb); self._nb.add(t,text=title); builder(t)

    # ══════════════════════════════════════════════════════════
    #  TAB 1 — PROFILO
    # ══════════════════════════════════════════════════════════
    def _tab_profilo(self, P):
        bar=ttk.Frame(P); bar.pack(fill="x",padx=4,pady=4)
        self._ck_prof=tk.BooleanVar(value=True)
        self._ck_base=tk.BooleanVar(value=True)
        self._ck_delt=tk.BooleanVar(value=True)
        self._ck_thresh=tk.BooleanVar(value=True)
        self._ck_nok=tk.BooleanVar(value=True)
        for var,txt in [(self._ck_prof,"Profilo"),(self._ck_base,"Baseline"),
                        (self._ck_delt,"Delta"),(self._ck_thresh,"Soglie"),
                        (self._ck_nok,"Celle NOK")]:
            tk.Checkbutton(bar,text=txt,variable=var,bg=DARK_BG,fg=TEXT_CLR,
                selectcolor="#1f6feb",activebackground=DARK_BG,font=("Consolas",9),
                command=self._draw_profilo).pack(side="left",padx=4)
        ttk.Button(bar,text="🔄",command=self._draw_all).pack(side="right",padx=2)
        ttk.Button(bar,text="💾 PNG",
                   command=lambda:self._save_plot(self.fig_p)).pack(side="right",padx=2)

        self.fig_p=Figure(figsize=(10,6),dpi=95,facecolor=DARK_BG)
        self.ax_p=self.fig_p.add_subplot(111,facecolor=PANEL_BG)
        self._sax(self.ax_p)
        cv=FigureCanvasTkAgg(self.fig_p,P)
        cv.get_tk_widget().pack(fill="both",expand=True,padx=4,pady=4)
        self._cv_p=cv
        tf=ttk.Frame(P); tf.pack(fill="x")
        tb=NavigationToolbar2Tk(cv,tf)
        tb.config(background=DARK_BG)
        for b in tb.winfo_children(): b.config(background=DARK_BG)
        tb.update()

    def _sax(self, ax):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=TEXT_CLR,which='both')
        for sp in ax.spines.values(): sp.set_color(BORDER_CLR)
        ax.grid(True,alpha=0.15,color=BORDER_CLR)

    def _draw_all(self): self._draw_profilo(); self._draw_delta()

    def _draw_profilo(self):
        ax=self.ax_p; ax.clear(); self._sax(ax)
        if self.db_data is None:
            ax.text(0.5,0.5,"Nessun dato — leggi dal PLC o apri un file .db",
                    ha='center',va='center',color=MUTED_CLR,fontsize=12,
                    transform=ax.transAxes)
            self._cv_p.draw_idle(); return

        sc=self.db_data.get('scalars',{}); ar=self.db_data.get('arrays',{})
        rc  = self._gs(sc,'I_ParametriCntrolloSpessore.RangeControllo','RangeControllo',default=80.0)
        pc  = self._gs(sc,'I_ParametriCntrolloSpessore.PosizioneCentroVentosa','PosizioneCentroVentosa',default=1930.0)
        sg  = self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreMassimo','SpessoreMassimo',default=1.0)
        sr  = self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento','SpessoreDiscoRiferimento',default=2.98)
        dl  = int(self._gs(sc,'I_DirLavoro',default=2))
        n   = ARRAY_SIZE
        x   = np.linspace(pc-rc, pc+rc, n)

        # Profilo spessore assoluto
        prof = ar.get('aProfiloSpessore',[])
        if len(prof)>=n and self._ck_prof.get():
            pa=np.array(prof[:n]); mask=np.abs(pa)>1e-6
            if mask.any():
                ax.plot(x[mask],pa[mask],color=PROFILE_CLR,lw=1.5,
                        label='Spessore assoluto [mm]',zorder=5)

        # Baseline (quota supporto)
        bas = ar.get('aBaseline',[])
        if len(bas)>=n and self._ck_base.get():
            ba=np.array(bas[:n]); mask=np.abs(ba)>1e-6
            if mask.any():
                ax.plot(x[mask],ba[mask],color=BASELINE_CLR,lw=1.0,ls='--',
                        alpha=0.8,label='Baseline (quota supporto)')

        # Delta
        dlt = ar.get('aProfiloDelta',[])
        if len(dlt)>=n and self._ck_delt.get():
            da=np.array(dlt[:n]); mask=np.abs(da)>1e-6
            if mask.any():
                ax.plot(x[mask],da[mask],color=DELTA_CLR,lw=1.2,alpha=0.8,
                        label=f'Delta vs rif. ({sr:.2f}mm)')

        # Soglie
        if self._ck_thresh.get():
            # Linea al livello di riferimento (spessore nominale)
            ax.axhline(sr, color=BASELINE_CLR, lw=0.8, ls=':', alpha=0.7,
                       label=f'Disco rif. ({sr:.2f}mm)')
            # Linea NOK assoluta = SpessoreDiscoRiferimento + SpessoreMassimo
            nok_abs = sr + sg
            ax.axhline(nok_abs, color=NOK_LINE_CLR, lw=1.2, ls=':',
                       alpha=0.9, label=f'Soglia NOK assoluta ({nok_abs:.2f}mm)')

        # Celle NOK: dove aProfiloDelta > SpessoreMassimo
        # (coincide con aProfiloSpessore > sr + sg)
        if self._ck_nok.get() and len(dlt)>=n:
            da=np.array(dlt[:n])
            # Usa aNraw per escludere celle non campionate
            nraw = ar.get('aNraw',[])
            mask_camp = np.array([1]*n)
            if len(nraw)>=n:
                mask_camp = np.array(nraw[:n]) > 0
            mask_fuori = mask_camp & (da > sg)
            if mask_fuori.any() and len(prof)>=n:
                pa=np.array(prof[:n])
                ax.scatter(x[mask_fuori],pa[mask_fuori],
                           color=FUORI_CLR,s=25,marker='x',lw=1.8,
                           label=f'Celle NOK: Δ>{sg:.2f}mm ({int(mask_fuori.sum())})',
                           zorder=8)

        ax.set_xlabel("Posizione asse [mm]",color=TEXT_CLR,fontsize=10)
        ax.set_ylabel("Spessore [mm]",color=TEXT_CLR,fontsize=10)

        nf  = int(sc.get('AppNcelleFuoriSoglia',sc.get('O_nCelleFuoriSoglia',0)))
        ns  = int(self._gs(sc,'I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme',default=15))
        dls = {0:"entrambe",1:"positive",2:"negative"}.get(dl,str(dl))
        fn  = self.db_data.get('filename','—')
        ts  = self.db_data.get('loaded_at')
        ttl = (f"{fn}   •   {ts.strftime('%H:%M:%S')}   •   "
               f"NOK: {nf}/{ns}   •   dir: {dls}") if ts else fn
        ax.set_title(ttl,color=ACCENT,fontsize=9,pad=6)
        ax.axvline(pc-rc,color=BORDER_CLR,lw=0.7,ls=':',alpha=0.5)
        ax.axvline(pc+rc,color=BORDER_CLR,lw=0.7,ls=':',alpha=0.5)
        ax.axvline(pc,color=BORDER_CLR,lw=0.5,ls='--',alpha=0.4)

        leg=ax.legend(loc='upper right',fontsize=8,framealpha=0.85,
                      facecolor=PANEL_BG,edgecolor=BORDER_CLR,labelcolor=TEXT_CLR)
        if leg: leg.get_frame().set_facecolor(PANEL_BG)
        self.fig_p.tight_layout(); self._cv_p.draw_idle()

    @staticmethod
    def _gs(sc,*keys,default=0.0):
        for k in keys:
            if k in sc:
                try: return float(sc[k])
                except: pass
        return default

    # ══════════════════════════════════════════════════════════
    #  TAB 2 — DELTA (eccesso rispetto al disco di riferimento)
    # ══════════════════════════════════════════════════════════
    def _tab_delta(self, P):
        bar=ttk.Frame(P); bar.pack(fill="x",padx=4,pady=4)
        ttk.Label(bar,
            text="Delta = spessore - disco_rif   •   NOK quando delta > SpessoreMassimo",
            style="Muted.TLabel").pack(side="left")
        ttk.Button(bar,text="🔄",command=self._draw_delta).pack(side="right",padx=2)
        ttk.Button(bar,text="💾 PNG",
                   command=lambda:self._save_plot(self.fig_d)).pack(side="right",padx=2)
        self.fig_d=Figure(figsize=(10,6),dpi=95,facecolor=DARK_BG)
        self.ax_d=self.fig_d.add_subplot(111,facecolor=PANEL_BG)
        self._sax(self.ax_d)
        cv=FigureCanvasTkAgg(self.fig_d,P)
        cv.get_tk_widget().pack(fill="both",expand=True,padx=4,pady=4)
        self._cv_d=cv

    def _draw_delta(self):
        ax=self.ax_d; ax.clear(); self._sax(ax)
        if self.db_data is None:
            ax.text(0.5,0.5,"Nessun dato",ha='center',va='center',
                    color=MUTED_CLR,fontsize=12,transform=ax.transAxes)
            self._cv_d.draw_idle(); return
        sc=self.db_data.get('scalars',{}); ar=self.db_data.get('arrays',{})
        rc  = self._gs(sc,'I_ParametriCntrolloSpessore.RangeControllo','RangeControllo',default=80.0)
        pc  = self._gs(sc,'I_ParametriCntrolloSpessore.PosizioneCentroVentosa','PosizioneCentroVentosa',default=1930.0)
        sr  = self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento','SpessoreDiscoRiferimento',default=2.98)
        sg  = self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreMassimo','SpessoreMassimo',default=1.0)
        n   = ARRAY_SIZE; x=np.linspace(pc-rc,pc+rc,n)

        dlt = ar.get('aProfiloDelta',[])
        if len(dlt)>=n:
            da=np.array(dlt[:n]); mask=np.abs(da)>1e-6
            if mask.any():
                xm=x[mask]; dm=da[mask]
                # Fill: rosso per eccesso (positivo = disco più spesso), blu per difetto
                ax.fill_between(xm,0,dm,where=(dm>=0),color=ERR_CLR,alpha=0.35,
                                label='Eccesso (disco più spesso del rif.)')
                ax.fill_between(xm,0,dm,where=(dm<0),color=ACCENT,alpha=0.25,
                                label='Difetto (disco più sottile del rif.)')
                ax.plot(xm,dm,color=DELTA_CLR,lw=1.2)
                # Evidenzia celle NOK (delta > SpessoreMassimo)
                mask_nok = mask & (da > sg)
                if mask_nok.any():
                    ax.fill_between(x[mask_nok],sg,da[mask_nok],
                                    color=FUORI_CLR,alpha=0.6,
                                    label=f'Zona NOK ({int(mask_nok.sum())} celle)')

        ax.axhline(0,color=MUTED_CLR,lw=1.0,ls='-',alpha=0.5)
        # Soglia NOK — il confronto nel PLC è aProfiloDelta > SpessoreMassimo
        ax.axhline(sg,color=NOK_LINE_CLR,lw=1.2,ls=':',alpha=0.9,
                   label=f'SpessoreMassimo (soglia NOK) = {sg:.2f}mm')

        ax.set_xlabel("Posizione asse [mm]",color=TEXT_CLR,fontsize=10)
        ax.set_ylabel("Delta spessore [mm]",color=TEXT_CLR,fontsize=10)
        ax.set_title(f"Eccesso spessore vs disco rif. ({sr:.3f}mm)  —  "
                     f"NOK se Δ > {sg:.2f}mm",
                     color=ACCENT,fontsize=10,pad=6)
        leg=ax.legend(loc='upper right',fontsize=8,framealpha=0.85,
                      facecolor=PANEL_BG,edgecolor=BORDER_CLR,labelcolor=TEXT_CLR)
        if leg: leg.get_frame().set_facecolor(PANEL_BG)
        self.fig_d.tight_layout(); self._cv_d.draw_idle()

    # ══════════════════════════════════════════════════════════
    #  TAB 3 — PLC READER
    # ══════════════════════════════════════════════════════════
    def _tab_plc(self, P):
        info_top=tk.Frame(P,bg=PANEL_BG,pady=3); info_top.pack(fill="x",padx=8,pady=(6,0))
        tk.Label(info_top,
            text=(f"✓  Db16070  S7_Optimized_Access := 'FALSE'  •  "
                  f"FB: {FB_TARGET}  •  {self._db_size} byte"),
            bg=PANEL_BG,fg=OK_CLR,font=("Consolas",9,"bold")).pack()

        if not SNAP7_AVAILABLE:
            self._no_snap7(P); return

        top=ttk.LabelFrame(P,text="  Connessione  ",padding=8)
        top.pack(fill="x",padx=8,pady=6)

        self._pv_ip  =tk.StringVar(value=self._cfg['PLC'].get('ip','192.168.0.1'))
        self._pv_rack=tk.StringVar(value=self._cfg['PLC'].get('rack','0'))
        self._pv_slot=tk.StringVar(value=self._cfg['PLC'].get('slot','1'))
        self._pv_db  =tk.StringVar(value=self._cfg['PLC'].get('db','16070'))

        r1=ttk.Frame(top); r1.pack(fill="x",pady=2)
        for lbl,var,w2 in [("IP:",self._pv_ip,16),("Rack:",self._pv_rack,4),
                            ("Slot:",self._pv_slot,4),("DB #:",self._pv_db,8)]:
            ttk.Label(r1,text=lbl,width=6).pack(side="left")
            ttk.Entry(r1,textvariable=var,width=w2,font=("Consolas",10)
                      ).pack(side="left",padx=2)

        r2=ttk.Frame(top); r2.pack(fill="x",pady=4)
        self._btn_conn=ttk.Button(r2,text="🔗 Connetti",style="Plc.TButton",
                                   command=self._plc_connect)
        self._btn_conn.pack(side="left",padx=2)
        self._btn_disc=ttk.Button(r2,text="❌ Disconnetti",
                                   command=self._plc_disconnect,state="disabled")
        self._btn_disc.pack(side="left",padx=2)
        self._btn_read=ttk.Button(r2,text="📥 Leggi DB",style="Plc.TButton",
                                   command=self._plc_read_auto)
        self._btn_read.pack(side="left",padx=8)
        self._btn_load=ttk.Button(r2,text="📊 Carica Viewer",style="Accent.TButton",
                                   command=self._plc_load_auto)
        self._btn_load.pack(side="left",padx=2)
        self._pv_plc_st=tk.StringVar(value="● Disconnesso")
        tk.Label(r2,textvariable=self._pv_plc_st,bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",9,"bold")).pack(side="right",padx=4)

        info=ttk.LabelFrame(P,text="  Info DB  ",padding=6)
        info.pack(fill="x",padx=8,pady=(0,6))
        tk.Label(info,
            text=(f"Db16070  •  {self._db_size} byte ({self._db_size/1024:.1f} KB)  •  "
                  f"{ARRAY_SIZE} celle [0..200]  •  0.80 mm/cella @ 80mm  •  "
                  f"I_SpessoreAtteso @ offset 68  •  AppSpessoreMedio @ 4970"),
            bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",9)).pack(anchor="w")

        log_lf=ttk.LabelFrame(P,text="  Log  ",padding=4)
        log_lf.pack(fill="both",expand=True,padx=8,pady=6)
        sb=ttk.Scrollbar(log_lf); sb.pack(side="right",fill="y")
        self._plc_log=tk.Text(log_lf,bg=DARK_BG,fg=TEXT_CLR,font=("Consolas",9),
                               wrap="word",yscrollcommand=sb.set,
                               insertbackground=TEXT_CLR,selectbackground=ACCENT)
        self._plc_log.pack(fill="both",expand=True)
        sb.config(command=self._plc_log.yview)
        for tag,clr in [("ok",OK_CLR),("err",ERR_CLR),("info",ACCENT),("warn",WARN_CLR)]:
            self._plc_log.tag_config(tag,foreground=clr)
        self._plog(f"=== PLC Reader {APP_RELEASE} ===\n","info")
        self._plog(f"Settings: {self._cfg_path}\n")
        self._plog(f"DB target: Db16070  •  FB: {FB_TARGET}\n")
        self._plog("Configura IP e premi Connetti.\n\n")

    def _no_snap7(self, P):
        w=ttk.Frame(P); w.pack(fill="both",expand=True,padx=20,pady=20)
        tk.Label(w,text="⚠ python-snap7 non installato",bg=DARK_BG,fg=WARN_CLR,
                 font=("Consolas",14,"bold")).pack(pady=10)
        tk.Label(w,text="pip install python-snap7",bg=DARK_BG,fg=TEXT_CLR,
                 font=("Consolas",11)).pack()

    def _plog(self, msg, tag=""):
        if hasattr(self,'_plc_log'):
            self._plc_log.insert("end",msg,tag); self._plc_log.see("end")

    def _plc_ensure_connected(self):
        """Assicura la connessione al PLC. Ritorna True se connesso."""
        if self._plc_client:
            try:
                # Verifica che la connessione sia ancora attiva
                if self._plc_client.client.get_connected():
                    return True
            except Exception:
                pass
        # Non connesso: connetti ora
        return self._plc_connect()

    def _plc_read_auto(self):
        """Leggi DB: si connette automaticamente se necessario."""
        if self._plc_ensure_connected():
            self._plc_read()

    def _plc_load_auto(self):
        """Carica Viewer: si connette, legge e carica in un click."""
        if self._plc_ensure_connected():
            self._plc_read()
            if self._plc_dec:
                self._plc_load()

    def _plc_connect(self):
        try:
            ip=self._pv_ip.get().strip()
            r,s=int(self._pv_rack.get() or 0),int(self._pv_slot.get() or 1)
            self._plog(f"→ {ip} rack={r} slot={s}...\n","info")
            self._plc_client=PLCReader(ip,r,s)
            cpu,pdu=self._plc_client.connect()
            self._plog(f"✓ CPU:{cpu}  PDU:{pdu}b\n","ok")
            self._pv_plc_st.set("● Connesso")
            self._btn_conn.config(state="disabled")
            self._btn_disc.config(state="normal")
            self._save_ini()
            return True
        except Exception as e:
            self._plog(f"✗ {e}\n","err")
            messagebox.showerror("Errore PLC",str(e))
            return False

    def _plc_disconnect(self):
        try:
            if self._plc_client: self._plc_client.disconnect()
        except Exception: pass
        self._plc_client=None
        self._pv_plc_st.set("● Disconnesso")
        self._btn_conn.config(state="normal"); self._btn_disc.config(state="disabled")
        self._plog("✓ Disconnesso.\n","ok")

    def _plc_read(self):
        if not self._plc_client: return
        try:
            db=int(self._pv_db.get())

            # 1. Rileva dimensione reale del DB con ricerca binaria
            self._plog(f"→ Rilevamento dimensione DB{db}...\n","info")
            actual=self._plc_client.detect_db_size(db, hint=max(self._db_size+200, 8000))
            if actual == 0:
                self._plog(f"✗ DB{db} non trovato o non accessibile.\n","err")
                return

            # Aggiorna lo stored actual size per l'auto-export
            self._actual_db_size = actual

            if actual < self._db_size:
                self._plog(
                    f"⚠ DB{db}: dimensione reale {actual} byte "
                    f"(stimata {self._db_size}b) — offset map parziale\n"
                    f"  Le variabili oltre byte {actual} saranno lette come zero.\n",
                    "warn")
            else:
                self._plog(f"✓ DB{db}: {actual} byte\n","ok")

            # 2. Legge il DB (con gestione automatica se più corto del previsto)
            t0=time.time()
            raw=self._plc_client.read_db_raw(db, self._db_size, actual_size=actual)
            dt=(time.time()-t0)*1000
            self._plog(f"✓ {min(actual,self._db_size)} byte letti in {dt:.0f}ms\n","ok")

            dec=decode_db(raw,self._omap); self._plc_dec=dec; sc=dec['scalars']

            # ── Diagnostica UDT: verifica che i primi 36 byte siano corretti ──
            pos_ctr = sc.get('I_ParametriCntrolloSpessore.PosizioneCentroVentosa')
            rng_ctr = sc.get('I_ParametriCntrolloSpessore.RangeControllo')
            sp_rif  = sc.get('I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento')
            sp_att  = sc.get('I_SpessoreAtteso')
            udt_ok  = (pos_ctr is not None
                       and 100.0 < pos_ctr < 5000.0
                       and rng_ctr is not None
                       and 1.0 < rng_ctr < 500.0)
            self._plog(
                f"  UDT: Pos={pos_ctr:.1f}mm  Range={rng_ctr:.1f}mm  "
                f"Rif={sp_rif:.3f}mm  Atteso={sp_att:.3f}mm\n",
                "ok" if udt_ok else "err")
            if not udt_ok:
                self._plog(
                    "  ✗ Valori UDT anomali — offset map non corretta\n","err")

            nf=int(sc.get('AppNcelleFuoriSoglia',0))
            ns=int(self._gs(sc,'I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme',default=15))
            self._plog(
                f"  Sp.med={sc.get('AppSpessoreMedio',0):.3f}  "
                f"Sp.max={sc.get('AppSpessoreMax',0):.3f}  "
                f"ΔMax={sc.get('AppDeltaMax',0):.3f}  "
                f"n={int(sc.get('AppNcelleProfilo',0))}  "
                f"fuori={nf}/{ns}\n","ok")
            if sc.get('AppSpessoreNok'):
                self._plog(f"  ⚠ DOPPIO SPESSORE\n","err")
            if sc.get('O_TaraturaAttiva'):
                self._plog("  ⚙ Taratura in corso\n","info")
            if not sc.get('AppValidValue',True):
                self._plog(f"  ⚠ Laser FUORI RANGE ({sc.get('I_Spessore_mm',0):.2f}mm)\n","warn")
            self._btn_load.config(state="normal")  # mantiene abilitato sempre
        except Exception as e:
            self._plog(f"✗ {e}\n","err")

    def _sql_save_decoded(self, decoded, db_num, is_tar=False, source="manuale"):
        """Salva decoded su SQLite. Usa la connessione auto-export se aperta,
        altrimenti apre una connessione temporanea."""
        sqlp = resolve_sql(getattr(self,'_pv_sql',
            tk.StringVar(value=self._cfg['SQL'].get('path','thickness_archive.sqlite'))).get())
        try:
            if self._ae_sql_con:
                con = self._ae_sql_con
                own = False
            else:
                con = sql_init(sqlp)
                own = True
            rid = sql_insert(con, decoded, db_num, is_tar)
            if own:
                n = sql_count(con); con.close()
                self.app_log(f"💾 Salvato su SQLite: #{rid}  ({n} righe totali)")
            else:
                try: self._pv_ae_rows.set(f"DB: {sql_count(con)} righe")
                except: pass
            return rid
        except Exception as e:
            self.app_log(f"Errore SQLite: {e}")
            return None

    def _plc_load(self):
        if not self._plc_dec: return
        db_num = int(self._pv_db.get())
        nm=f"DB{db_num}_{datetime.datetime.now().strftime('%H%M%S')}"
        # Carica nel viewer
        self._load_data(parse_db_text(gen_db_text(self._plc_dec,nm),nm+".db"))
        self._plog("✓ Viewer aggiornato.\n","ok")
        # Salva su SQLite
        sc = self._plc_dec['scalars']
        is_tar = bool(sc.get('O_TaraturaAttiva',False)) or bool(sc.get('taraturaInCorso',False))
        rid = self._sql_save_decoded(self._plc_dec, db_num, is_tar, source="manuale")
        if rid:
            self._plog(f"💾 Salvato su SQLite: #{rid}\n","ok")
        self._nb.select(0)

    # ══════════════════════════════════════════════════════════
    #  TAB 4 — AUTO-EXPORT  (multi-DB, fino a 10 slots)
    # ══════════════════════════════════════════════════════════
    def _tab_autoexp(self, P):
        if not SNAP7_AVAILABLE:
            self._no_snap7(P); return

        pane=ttk.PanedWindow(P,orient="horizontal")
        pane.pack(fill="both",expand=True)
        left=ttk.Frame(pane,width=430); pane.add(left,weight=0); left.pack_propagate(False)
        right=ttk.Frame(pane); pane.add(right,weight=1)

        # ── Configurazione globale ────────────────────────────
        lf_cfg=ttk.LabelFrame(left,text="  Configurazione  ",padding=6)
        lf_cfg.pack(fill="x",padx=6,pady=4)
        r1=ttk.Frame(lf_cfg); r1.pack(fill="x",pady=2)
        ttk.Label(r1,text="Poll:",width=8).pack(side="left")
        self._pv_poll=tk.StringVar(value="50")
        ttk.Entry(r1,textvariable=self._pv_poll,width=8).pack(side="left",padx=2)
        ttk.Label(r1,text="ms  per slot (lettura 4 byte IdLettura)",
                  style="Muted.TLabel").pack(side="left")

        # ── Lista DB slots ────────────────────────────────────
        lf_db=ttk.LabelFrame(left,text="  DB da monitorare  (☑ = abilitato)  ",padding=6)
        lf_db.pack(fill="x",padx=6,pady=4)

        # Header colonne
        hdr=ttk.Frame(lf_db); hdr.pack(fill="x",pady=(0,2))
        tk.Label(hdr,text="On",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8),width=4).pack(side="left")
        tk.Label(hdr,text="DB #",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8),width=8).pack(side="left")
        tk.Label(hdr,text="Descrizione",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8),width=18).pack(side="left")
        tk.Label(hdr,text="Ultima passata",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8)).pack(side="left",padx=(4,0))

        # 10 slots
        N_SLOTS = 10
        self._ae_db_slots = []
        # Default: primo slot = DB dal PLC Reader, resto vuoti
        defaults = [('16070','ST1015 Pinza Bassa')] + [('','')] * (N_SLOTS-1)

        for i,(db_def,lbl_def) in enumerate(defaults):
            row=ttk.Frame(lf_db); row.pack(fill="x",pady=1)
            slot={}

            # Checkbox abilitazione
            slot['enabled']=tk.BooleanVar(value=(i==0))
            cb=tk.Checkbutton(row,variable=slot['enabled'],bg=DARK_BG,
                fg=OK_CLR,selectcolor="#1f6feb",activebackground=DARK_BG,
                width=2)
            cb.pack(side="left")

            # DB number
            slot['db_num']=tk.StringVar(value=db_def)
            ttk.Entry(row,textvariable=slot['db_num'],width=8,
                      font=("Consolas",9)).pack(side="left",padx=2)

            # Label
            slot['label']=tk.StringVar(value=lbl_def)
            ttk.Entry(row,textvariable=slot['label'],width=18,
                      font=("Consolas",8)).pack(side="left",padx=2)

            # Stato ultima passata (aggiornato dal poll)
            slot['status']=tk.StringVar(value="—")
            tk.Label(row,textvariable=slot['status'],bg=DARK_BG,
                     fg=MUTED_CLR,font=("Consolas",8),anchor="w",
                     width=20).pack(side="left",padx=(4,0))

            # Counter precedente (runtime, non visibile)
            slot['prev_counter'] = None
            slot['ok_count']  = 0
            slot['nok_count'] = 0
            slot['tar_count'] = 0

            self._ae_db_slots.append(slot)

        # Bottone copia DB dal PLC Reader nel primo slot libero
        btn_row=ttk.Frame(lf_db); btn_row.pack(fill="x",pady=(4,0))
        ttk.Button(btn_row,text="← Copia DB da PLC Reader",
                   command=self._ae_copy_db_from_plc).pack(side="left",padx=2)
        ttk.Button(btn_row,text="✗ Deseleziona tutti",
                   command=self._ae_disable_all).pack(side="left",padx=2)

        # ── SQLite ────────────────────────────────────────────
        lf_sql=ttk.LabelFrame(left,text="  Archivio SQLite  ",padding=6)
        lf_sql.pack(fill="x",padx=6,pady=4)
        tk.Label(lf_sql,text="Percorso (da Impostazioni):",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8)).pack(anchor="w")
        self._pv_ae_sql=tk.StringVar(value="—")
        tk.Label(lf_sql,textvariable=self._pv_ae_sql,bg=DARK_BG,fg=ACCENT,
                 font=("Consolas",9),wraplength=380,anchor="w",
                 justify="left").pack(anchor="w",pady=2)

        # ── Opzioni ───────────────────────────────────────────
        lf_opt=ttk.LabelFrame(left,text="  Opzioni  ",padding=6)
        lf_opt.pack(fill="x",padx=6,pady=4)
        self._pv_ae_viewer=tk.BooleanVar(value=True)
        self._pv_ae_tar=tk.BooleanVar(value=True)
        for var,txt in [(self._pv_ae_viewer,"Carica automaticamente nel Viewer"),
                        (self._pv_ae_tar,"Archivia anche le tarature")]:
            tk.Checkbutton(lf_opt,text=txt,variable=var,bg=DARK_BG,fg=TEXT_CLR,
                selectcolor="#1f6feb",activebackground=DARK_BG,font=("Consolas",9),
                anchor="w").pack(fill="x",pady=1)

        # ── Monitoraggio ──────────────────────────────────────
        lf_mon=ttk.LabelFrame(left,text="  Monitoraggio  ",padding=6)
        lf_mon.pack(fill="x",padx=6,pady=4)
        rr=ttk.Frame(lf_mon); rr.pack(fill="x")
        self._btn_ae_start=tk.Button(rr,text="▶ Avvia",bg=OK_CLR,fg=DARK_BG,
            font=("Consolas",10,"bold"),command=self._ae_start,width=10)
        self._btn_ae_start.pack(side="left",padx=2)
        self._btn_ae_stop=tk.Button(rr,text="■ Stop",bg=ERR_CLR,fg=DARK_BG,
            font=("Consolas",10,"bold"),command=self._ae_stop,state="disabled",width=10)
        self._btn_ae_stop.pack(side="left",padx=2)
        self._pv_ae_st=tk.StringVar(value="● Fermo")
        tk.Label(lf_mon,textvariable=self._pv_ae_st,bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",10,"bold")).pack(anchor="w",pady=4)
        self._pv_ae_cnt=tk.StringVar(value="✓ 0  ✗ 0  ⚙ 0")
        tk.Label(lf_mon,textvariable=self._pv_ae_cnt,bg=DARK_BG,fg=AUTOEXP_CLR,
                 font=("Consolas",11,"bold")).pack(anchor="w")
        self._pv_ae_last=tk.StringVar(value="")
        tk.Label(lf_mon,textvariable=self._pv_ae_last,bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8)).pack(anchor="w")
        self._pv_ae_rows=tk.StringVar(value="DB: — righe")
        tk.Label(lf_mon,textvariable=self._pv_ae_rows,bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8)).pack(anchor="w",pady=(4,0))

        # ── Log ───────────────────────────────────────────────
        log_lf=ttk.LabelFrame(right,text="  Log Auto-Export  ",padding=4)
        log_lf.pack(fill="both",expand=True,padx=6,pady=6)
        sb=ttk.Scrollbar(log_lf); sb.pack(side="right",fill="y")
        self._ae_log=tk.Text(log_lf,bg=DARK_BG,fg=TEXT_CLR,font=("Consolas",9),
                              wrap="word",yscrollcommand=sb.set,
                              insertbackground=TEXT_CLR,selectbackground=ACCENT)
        self._ae_log.pack(fill="both",expand=True)
        sb.config(command=self._ae_log.yview)
        for tag,clr in [("ok",OK_CLR),("err",ERR_CLR),("info",ACCENT),
                        ("warn",WARN_CLR),("tar",TAR_CLR)]:
            self._ae_log.tag_config(tag,foreground=clr)
        self._alog("=== Auto-Export (multi-DB) ===\n","info")
        self._alog("Configura i DB slots, poi premi Avvia.\n\n")
        self._upd_ae_sql()

    def _ae_copy_db_from_plc(self):
        """Copia il DB number dal PLC Reader nel primo slot vuoto."""
        db_val = self._pv_db.get().strip() if hasattr(self,'_pv_db') else ''
        if not db_val: return
        for slot in self._ae_db_slots:
            if not slot['db_num'].get().strip():
                slot['db_num'].set(db_val)
                slot['enabled'].set(True)
                return
        # Se non c'è slot vuoto, mette nel primo
        self._ae_db_slots[0]['db_num'].set(db_val)
        self._ae_db_slots[0]['enabled'].set(True)

    def _ae_disable_all(self):
        for slot in self._ae_db_slots:
            slot['enabled'].set(False)

    def _upd_ae_sql(self):
        raw=self._cfg['SQL'].get('path','thickness_archive.sqlite')
        if hasattr(self,'_pv_sql'): raw=self._pv_sql.get()
        if hasattr(self,'_pv_ae_sql'): self._pv_ae_sql.set(resolve_sql(raw))

    def _alog(self, msg, tag=""):
        if hasattr(self,'_ae_log'):
            self._ae_log.insert("end",msg,tag); self._ae_log.see("end")

    def _ae_start(self):
        # Verifica che almeno uno slot sia abilitato
        active_slots = [s for s in self._ae_db_slots
                        if s['enabled'].get() and s['db_num'].get().strip()]
        if not active_slots:
            messagebox.showwarning("Nessun DB","Abilita almeno un DB nella lista.")
            return

        # Auto-connessione se necessario
        if not self._plc_client or not self._plc_client.client.get_connected():
            self._alog("→ Auto-connessione PLC...\n","info")
            if not self._plc_ensure_connected():
                return
        self._ae_client = self._plc_client

        sqlp=resolve_sql(getattr(self,'_pv_sql',
             tk.StringVar(value=self._cfg['SQL'].get('path','thickness_archive.sqlite'))).get())
        try:
            self._ae_sql_con=sql_init(sqlp)
            n0=sql_count(self._ae_sql_con)
            self._alog(f"✓ SQLite: {sqlp}\n  {n0} righe esistenti\n","ok")
            self._pv_ae_rows.set(f"DB: {n0} righe")
        except Exception as e:
            messagebox.showerror("Errore SQLite",str(e)); return

        # Reset contatori e stato per ogni slot
        for slot in self._ae_db_slots:
            slot['prev_counter'] = None
            slot['ok_count']  = 0
            slot['nok_count'] = 0
            slot['tar_count'] = 0
            slot['status'].set("⏳ in attesa...")

        self._ae_running=True
        self._ae_cok=self._ae_cnok=self._ae_ctar=0
        self._pv_ae_cnt.set("✓ 0  ✗ 0  ⚙ 0"); self._pv_ae_st.set("● Attivo")
        self._btn_ae_start.config(state="disabled"); self._btn_ae_stop.config(state="normal")
        if hasattr(self,'_lbl_ae_badge'):
            self._lbl_ae_badge.config(text="⚡ AUTO-EXPORT: ON",fg=AUTOEXP_CLR)
        dbs_txt = ", ".join([f"DB{s['db_num'].get()}" for s in active_slots])
        self._alog(f"▶ poll={self._pv_poll.get()}ms  DB monitorati: {dbs_txt}\n","info")
        self._ae_poll()

    def _ae_stop(self):
        self._ae_running=False
        if self._ae_timer:
            try: self.after_cancel(self._ae_timer)
            except: pass
            self._ae_timer=None
        if self._ae_sql_con:
            try: self._ae_sql_con.close()
            except: pass
            self._ae_sql_con=None
        self._btn_ae_start.config(state="normal"); self._btn_ae_stop.config(state="disabled")
        self._pv_ae_st.set("● Fermo"); self._alog("■ Fermato.\n\n","warn")
        if hasattr(self,'_lbl_ae_badge'):
            self._lbl_ae_badge.config(text="⚡ AUTO-EXPORT: OFF",fg=MUTED_CLR)

    def _ae_poll(self):
        if not self._ae_running: return
        try:
            for slot in self._ae_db_slots:
                if not slot['enabled'].get(): continue
                db_str = slot['db_num'].get().strip()
                if not db_str: continue
                try:
                    db = int(db_str)
                except ValueError:
                    continue

                # Leggi solo i 4 byte del counter
                counter = self._ae_client.read_counter(db, offset=4996)

                if counter is None:
                    slot['status'].set("⚠ errore lettura")
                    continue

                if slot['prev_counter'] is None:
                    slot['prev_counter'] = counter
                    slot['status'].set(f"⏳ #{counter}")
                    continue

                if counter != slot['prev_counter']:
                    slot['prev_counter'] = counter
                    # Lettura completa del DB
                    raw = self._ae_client.read_db_raw(
                        db, self._db_size, actual_size=self._actual_db_size)
                    dec = decode_db(raw, self._omap)
                    self._ae_trigger(dec, db, counter, slot)

        except Exception as e:
            self._alog(f"✗ polling: {e}\n","err")
            self._pv_ae_st.set("● Errore")

        if self._ae_running:
            try: ms = max(20, int(self._pv_poll.get()))
            except: ms = 50
            self._ae_timer = self.after(ms, self._ae_poll)

    def _ae_trigger(self, dec, db, counter=0, slot=None):
        sc=dec['scalars']; ts=datetime.datetime.now()
        is_tar=(bool(sc.get('O_TaraturaAttiva',False)) or
                bool(sc.get('taraturaInCorso',False)))
        is_nok=bool(sc.get('AppSpessoreNok',sc.get('O_SpessoreNOK',False)))
        if is_tar and not self._pv_ae_tar.get(): return
        if is_tar:   prefix,tag="TAR","tar"; self._ae_ctar+=1
        elif is_nok: prefix,tag="NOK","err"; self._ae_cnok+=1
        else:        prefix,tag="OK ","ok";  self._ae_cok+=1
        try:
            rid=sql_insert(self._ae_sql_con,dec,db,is_tar)
        except Exception as e:
            self._alog(f"✗ SQLite: {e}\n","err"); return
        icon="⚙" if is_tar else ("✗" if is_nok else "✓")
        nf=int(sc.get('AppNcelleFuoriSoglia',sc.get('O_nCelleFuoriSoglia',0)))
        sp_med=sc.get('AppSpessoreMedio',0)
        dmax=sc.get('AppDeltaMax',0)
        ncelle=int(sc.get('AppNcelleProfilo',0))

        # Label DB nello slot
        db_lbl = ""
        if slot:
            lbl=slot['label'].get().strip()
            db_lbl=f"[{lbl}] " if lbl else ""
            if is_tar:   slot['tar_count']+=1
            elif is_nok: slot['nok_count']+=1
            else:        slot['ok_count']+=1
            slot['status'].set(
                f"{icon} #{counter}  "
                f"Sp={sp_med:.2f}  "
                f"✓{slot['ok_count']} ✗{slot['nok_count']}")

        self._alog(
            f"{icon} DB{db} {db_lbl}[{ts.strftime('%H:%M:%S')}] "
            f"#{counter}  "
            f"Sp={sp_med:.3f}  ΔMax={dmax:.3f}  "
            f"n={ncelle:>3d}  fuori={nf:>3d}  sql#{rid}\n",tag)

        self._pv_ae_cnt.set(
            f"✓ {self._ae_cok}  ✗ {self._ae_cnok}  ⚙ {self._ae_ctar}")
        self._pv_ae_last.set(f"{icon} DB{db} {ts.strftime('%H:%M:%S')}  #{rid}")
        try: self._pv_ae_rows.set(f"DB: {sql_count(self._ae_sql_con)} righe")
        except: pass
        if self._pv_ae_viewer.get():
            try:
                txt=gen_db_text(dec,f"DB{db}_#{rid}_{prefix}")
                self._load_data(parse_db_text(txt,f"DB{db} #{rid}"))
            except: pass

    # ══════════════════════════════════════════════════════════
    #  TAB 5 — HISTORY
    # ══════════════════════════════════════════════════════════
    def _tab_history(self, P):
        bar=ttk.Frame(P); bar.pack(fill="x",padx=6,pady=6)
        ttk.Label(bar,text="Filtro:").pack(side="left")
        self._pv_hf=tk.StringVar(value="Tutti")
        cmb=ttk.Combobox(bar,textvariable=self._pv_hf,width=15,state="readonly",
            values=["Tutti","Solo OK","Solo NOK","Solo Tarature"])
        cmb.current(0); cmb.pack(side="left",padx=4)
        cmb.bind("<<ComboboxSelected>>",lambda e:self._hist_refresh())
        ttk.Label(bar,text="Limite:").pack(side="left",padx=(10,2))
        self._pv_hl=tk.StringVar(value="500")
        ttk.Entry(bar,textvariable=self._pv_hl,width=6).pack(side="left",padx=2)
        ttk.Button(bar,text="🔄 Aggiorna",command=self._hist_refresh).pack(side="left",padx=4)
        self._pv_hi=tk.StringVar(value="0 righe")
        tk.Label(bar,textvariable=self._pv_hi,bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",9)).pack(side="right",padx=6)

        lf=ttk.LabelFrame(P,text="  Archivio  ",padding=4)
        lf.pack(fill="both",expand=True,padx=6,pady=6)
        cols=("id","time","db","verdict","medio","max","delta_max","n","fuori")
        tree=ttk.Treeview(lf,columns=cols,show="headings",height=22)
        for c,txt,w2,anc in [("id","ID",60,"e"),("time","Timestamp",170,"w"),
                               ("db","DB#",55,"e"),("verdict","Esito",80,"center"),
                               ("medio","Sp.med",80,"e"),("max","Sp.max",80,"e"),
                               ("delta_max","Δmax",75,"e"),("n","N.cel",55,"e"),
                               ("fuori","NOK",55,"e")]:
            tree.heading(c,text=txt); tree.column(c,width=w2,anchor=anc)
        tree.tag_configure('ok',foreground=OK_CLR)
        tree.tag_configure('nok',foreground=ERR_CLR)
        tree.tag_configure('tar',foreground=TAR_CLR)
        sb=ttk.Scrollbar(lf,command=tree.yview)
        tree.configure(yscrollcommand=sb.set); sb.pack(side="right",fill="y")
        tree.pack(fill="both",expand=True)
        tree.bind("<Double-1>",lambda e:self._hist_open())
        self._hist_tree=tree

        bar2=ttk.Frame(P); bar2.pack(fill="x",padx=6,pady=4)
        ttk.Button(bar2,text="📊 Apri nel Viewer",style="Accent.TButton",
                   command=self._hist_open).pack(side="left",padx=2)
        ttk.Button(bar2,text="🗑 Elimina",
                   command=self._hist_del).pack(side="left",padx=2)

    def _sql_path(self):
        return resolve_sql(getattr(self,'_pv_sql',
            tk.StringVar(value=self._cfg['SQL'].get('path','thickness_archive.sqlite'))).get())

    def _hist_refresh(self):
        sqlp=self._sql_path()
        if not os.path.isfile(sqlp):
            self._hist_tree.delete(*self._hist_tree.get_children())
            self._pv_hi.set("archivio non trovato"); return
        try:
            con=sql_init(sqlp)
            fm={"Tutti":None,"Solo OK":'ok',"Solo NOK":'nok',"Solo Tarature":'tar'}
            limit=int(self._pv_hl.get()) if self._pv_hl.get().isdigit() else 500
            rows=sql_query(con,limit,fm.get(self._pv_hf.get())); con.close()
        except Exception as e:
            messagebox.showerror("Query",str(e)); return
        self._hist_tree.delete(*self._hist_tree.get_children())
        for r in rows:
            rid,ts,dbn,sm,smx,dm,dmx,n,nf,ok,nok,tar=r
            if tar: verdict,tag="TARAT","tar"
            elif nok: verdict,tag="NOK","nok"
            elif ok: verdict,tag="OK","ok"
            else: verdict,tag="—",""
            self._hist_tree.insert("","end",
                values=(rid,ts,dbn,verdict,f"{sm:.3f}",f"{smx:.3f}",
                        f"{dmx:.3f}",n,nf or 0),
                tags=(tag,),iid=str(rid))
        self._pv_hi.set(f"{len(rows)} righe")

    def _hist_open(self):
        sel=self._hist_tree.selection()
        if not sel: return
        rid=int(sel[0])
        try:
            con=sqlite3.connect(self._sql_path())
            txt=sql_raw(con,rid); con.close()
            if not txt: messagebox.showwarning("Vuoto",f"#{rid} vuota."); return
            self._load_data(parse_db_text(txt,f"#{rid}")); self._nb.select(0)
        except Exception as e: messagebox.showerror("Errore",str(e))

    def _hist_del(self):
        sel=self._hist_tree.selection()
        if not sel: return
        if not messagebox.askyesno("Conferma",f"Eliminare {len(sel)} righe?"): return
        try:
            con=sqlite3.connect(self._sql_path())
            for iid in sel: sql_delete(con,int(iid))
            con.close()
        except Exception as e: messagebox.showerror("Errore",str(e)); return
        self._hist_refresh()

    # ══════════════════════════════════════════════════════════
    #  TAB 6 — IMPOSTAZIONI
    # ══════════════════════════════════════════════════════════
    def _tab_settings(self, P):
        wrap=ttk.Frame(P); wrap.pack(fill="both",expand=True,padx=12,pady=12)
        ttk.Label(wrap,text="Impostazioni applicazione",
                  style="Title.TLabel").pack(anchor="w",pady=(0,8))

        lfi=ttk.LabelFrame(wrap,text="  File di setup  ",padding=8)
        lfi.pack(fill="x",pady=4)
        for r,(lbl,val) in enumerate([("INI:",self._cfg_path),("App dir:",get_app_dir())]):
            tk.Label(lfi,text=lbl,bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",9),
                     anchor="w",width=10).grid(row=r,column=0,sticky="w",padx=2,pady=2)
            tk.Label(lfi,text=val,bg=DARK_BG,fg=ACCENT,font=("Consolas",9),
                     wraplength=600,justify="left",anchor="w"
                     ).grid(row=r,column=1,sticky="w",padx=4,pady=2)

        plc_lf=ttk.LabelFrame(wrap,text="  PLC  ",padding=8); plc_lf.pack(fill="x",pady=6)
        if not hasattr(self,'_pv_ip'):
            self._pv_ip  =tk.StringVar(value=self._cfg['PLC'].get('ip','192.168.0.1'))
            self._pv_rack=tk.StringVar(value=self._cfg['PLC'].get('rack','0'))
            self._pv_slot=tk.StringVar(value=self._cfg['PLC'].get('slot','1'))
            self._pv_db  =tk.StringVar(value=self._cfg['PLC'].get('db','16010'))
        for r,(lbl,var,w2) in enumerate([
                ("IP:",self._pv_ip,20),("Rack:",self._pv_rack,6),
                ("Slot:",self._pv_slot,6),("DB #:",self._pv_db,10)]):
            tk.Label(plc_lf,text=lbl,bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",10),
                     anchor="w",width=10).grid(row=r,column=0,sticky="w",padx=2,pady=2)
            ttk.Entry(plc_lf,textvariable=var,width=w2,font=("Consolas",10)
                      ).grid(row=r,column=1,sticky="w",padx=4,pady=2)

        sql_lf=ttk.LabelFrame(wrap,text="  Archivio SQLite  ",padding=8)
        sql_lf.pack(fill="x",pady=6)
        self._pv_sql=tk.StringVar(value=self._cfg['SQL'].get('path','thickness_archive.sqlite'))
        tk.Label(sql_lf,text="File SQLite:",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",10),anchor="w",width=10
                 ).grid(row=0,column=0,sticky="w",padx=2,pady=2)
        ttk.Entry(sql_lf,textvariable=self._pv_sql,width=60,font=("Consolas",10)
                  ).grid(row=0,column=1,sticky="ew",padx=4,pady=2)
        ttk.Button(sql_lf,text="📁",command=self._browse_sql
                   ).grid(row=0,column=2,padx=2)
        tk.Label(sql_lf,text="(relativo = cartella app; assoluto = path completo)",
                 bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",8)
                 ).grid(row=1,column=0,columnspan=3,sticky="w",padx=2)
        self._pv_sql_res=tk.StringVar(value=f"→ {resolve_sql(self._pv_sql.get())}")
        tk.Label(sql_lf,textvariable=self._pv_sql_res,bg=DARK_BG,fg=ACCENT,
                 font=("Consolas",9),wraplength=700,anchor="w",justify="left"
                 ).grid(row=2,column=0,columnspan=3,sticky="w",padx=2,pady=2)
        self._pv_sql.trace_add('write',lambda *a:self._upd_sql_res())

        btn_lf=ttk.Frame(wrap); btn_lf.pack(fill="x",pady=10)
        ttk.Button(btn_lf,text="💾 Salva",style="Accent.TButton",
                   command=self._save_confirm).pack(side="left",padx=2)
        ttk.Button(btn_lf,text="📂 Apri cartella",
                   command=lambda:self._open_path(get_app_dir())).pack(side="left",padx=2)

        notes_lf=ttk.LabelFrame(wrap,text="  Note tecniche  ",padding=8)
        notes_lf.pack(fill="both",expand=True,pady=4)
        notes=tk.Text(notes_lf,bg=PANEL_BG,fg=TEXT_CLR,font=("Consolas",9),
                      wrap="word",height=14,insertbackground=TEXT_CLR)
        notes.pack(fill="both",expand=True)
        notes.insert("end",f"""
{FB_TARGET}  v1.2  |  DB: {self._db_size} byte ({self._db_size/1024:.1f} KB)
Array: {ARRAY_SIZE} celle [0..200]  |  Risoluzione: 0.80 mm/cella @ RangeControllo=80mm
Offset map verificata empiricamente su CPU 1517F-3 PN/DP (VAR_IN_OUT=2b, DB v1.1=4992b)

NOVITÀ v1.2 — SEPARAZIONE TARATURA / PRODUZIONE:
  SpessoreDiscoRiferimento [UDT]:
    Usato SOLO per costruire la baseline durante la taratura.
    Non cambia finché non si ritara il banco.
  I_SpessoreAtteso [VAR_INPUT, offset 68]:
    Spessore nominale del disco di produzione corrente.
    Cambia ad ogni cambio formato (1.1mm, 2.1mm, 3.1mm...).
    NON richiede ritaratura.
  SpessoreMassimo [UDT]:
    Tolleranza massima su aProfiloDelta [mm].
    NOK se aProfiloSpessore > I_SpessoreAtteso + SpessoreMassimo.

OFFSET MAP v1.2 (offset chiave):
  I_SpessoreAtteso        @  68  Real
  O_SpessoreMedio         @  74  Real
  Pinza.OvrAutoOld        @ 102  LReal
  aBaseline[0]            @ 122  Real (201 x 4 = 804 byte)
  aSomRaw[0]              @ 948
  aProfiloSpessore[0]     @ 3360
  aProfiloDelta[0]        @ 4164
  AppSpessoreMedio        @ 4970  Real
  AppNcelleFuoriSoglia    @ 4988  Int
  OldDirLavoro            @ 4992  Int

FISICA: laser SOTTO il disco, quota decresce con spessore crescente.
  aBaseline[i] = laser_cal + SpessoreDiscoRiferimento  = quota supporto
  aProfiloSpessore[i] = aBaseline[i] - laser_prod      = spessore reale
  aProfiloDelta[i]    = aProfiloSpessore[i] - I_SpessoreAtteso = eccesso
""")
        notes.config(state="disabled")

    def _upd_sql_res(self):
        try:
            self._pv_sql_res.set(f"→ {resolve_sql(self._pv_sql.get())}")
            self._upd_ae_sql()
        except: pass

    def _save_confirm(self):
        self._save_ini()
        messagebox.showinfo("Salvato",f"INI: {self._cfg_path}")

    def _browse_sql(self):
        init=resolve_sql(self._pv_sql.get())
        fp=filedialog.asksaveasfilename(title="SQLite",defaultextension=".sqlite",
            initialfile=os.path.basename(init),
            initialdir=os.path.dirname(init) or get_app_dir(),
            filetypes=[("SQLite","*.sqlite *.db3"),("Tutti","*.*")])
        if fp:
            try:
                rel=os.path.relpath(fp,get_app_dir())
                self._pv_sql.set(rel if not rel.startswith('..') else fp)
            except: self._pv_sql.set(fp)

    # ── HELPERS ──────────────────────────────────────────────
    def app_log(self, msg, *_): self._lbl_st.config(text=msg)

    def _open_file(self):
        fp=filedialog.askopenfilename(title="Apri .db",initialdir=get_app_dir(),
            filetypes=[("TIA DB","*.db"),("Tutti","*.*")])
        if not fp: return
        try: self._load_data(parse_db_file(fp))
        except Exception as e: messagebox.showerror("Errore parser",str(e))

    def _save_plot(self, fig):
        fp=filedialog.asksaveasfilename(title="Salva PNG",defaultextension=".png",
            initialdir=get_app_dir(),
            filetypes=[("PNG","*.png"),("SVG","*.svg"),("PDF","*.pdf")])
        if not fp: return
        try: fig.savefig(fp,dpi=150,facecolor=DARK_BG,bbox_inches='tight')
        except Exception as e: messagebox.showerror("Errore",str(e))

    def _open_path(self, path):
        try:
            if sys.platform=="win32": os.startfile(path)
            elif sys.platform=="darwin":
                import subprocess; subprocess.Popen(["open",path])
            else:
                import subprocess; subprocess.Popen(["xdg-open",path])
        except: pass

    def _load_data(self, data):
        self.db_data=data
        self._lbl_file.config(text=data.get('filename','—'))
        self._upd_panel()
        self._draw_all()
        self.app_log(f"Caricato: {data.get('filename','—')}")

    def _upd_panel(self):
        if not self.db_data: return
        sc=self.db_data.get('scalars',{})

        # Verdict
        nok=bool(sc.get('AppSpessoreNok',sc.get('O_SpessoreNOK',False)))
        dis=bool(sc.get('I_ParametriCntrolloSpessore.DisabilitaControllo',
                        sc.get('DisabilitaControllo',False)))
        if dis:
            self._pv_verd.set("⚠ DISABILITATO"); self._lbl_verd.config(fg=WARN_CLR)
        elif nok:
            self._pv_verd.set("✗ DOPPIO SPESSORE"); self._lbl_verd.config(fg=ERR_CLR)
        else:
            self._pv_verd.set("✓ OK"); self._lbl_verd.config(fg=OK_CLR)

        # Spessore atteso produzione (v1.2)
        sp_att = sc.get('I_SpessoreAtteso', None)
        sp_max_tol = self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreMassimo',default=1.0)
        if sp_att is not None:
            self._pv_sp_att.set(f"{sp_att:.3f} mm")
            nok_abs = sp_att + sp_max_tol
            self._lbl_sp_att.config(
                text=f"{sp_att:.3f} mm  (NOK > {nok_abs:.3f} mm)")
        else:
            self._pv_sp_att.set("—")

        # Celle fuori soglia (v1.1: legge AppNcelleFuoriSoglia da VAR)
        nf=int(sc.get('AppNcelleFuoriSoglia',sc.get('O_nCelleFuoriSoglia',0)))
        ns=int(self._gs(sc,'I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme',default=15))
        sg=self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreMassimo',default=1.0)
        self._pv_fuori.set(f"Celle Δ>{sg:.2f}mm: {nf} / {ns}")
        self._lbl_fuori.config(fg=(ERR_CLR if nf>=ns else WARN_CLR if nf>0 else OK_CLR))

        # Scalari
        self._pvs['SpessoreMedio'].set(f"{sc.get('AppSpessoreMedio',sc.get('O_SpessoreMedio',0)):.3f} mm")
        self._pvs['SpessoreMax'].set(f"{sc.get('AppSpessoreMax',sc.get('O_SpessoreMax',0)):.3f} mm")
        self._pvs['DeltaMedio'].set(f"{sc.get('AppDeltaMedio',sc.get('O_DeltaMedio',0)):.3f} mm")
        self._pvs['DeltaMax'].set(f"{sc.get('AppDeltaMax',sc.get('O_DeltaMax',0)):.3f} mm")
        self._pvs['nCelleProfilo'].set(f"{int(sc.get('AppNcelleProfilo',sc.get('O_nCelleProfilo',0)))} / {ARRAY_SIZE}")

        # Taratura
        ta=bool(sc.get('O_TaraturaAttiva',sc.get('taraturaInCorso',False)))
        tv=bool(sc.get('O_TaraturaCompletata',sc.get('baselineValida',False)))
        self._pv_tar.set("⚙ Taratura in corso..." if ta else
                         ("✓ Baseline valida" if tv else "⚠ Baseline NON tarata"))
        sr=sc.get('I_ParametriCntrolloSpessore.SpessoreDiscoRiferimento',
                  sc.get('SpessoreDiscoRiferimento',0.0))
        self._pv_tar_rif.set(f"Riferimento: {sr:.3f} mm")

        # Parametri UDT
        for key,var in self._pv_par.items():
            for k in [f'I_ParametriCntrolloSpessore.{key}',key]:
                v=sc.get(k)
                if v is not None:
                    var.set("SÌ" if v is True else "NO" if v is False else
                            f"{v:.3f}" if isinstance(v,float) else str(v))
                    break
            else: var.set("—")

        # Laser
        lmin=sc.get('I_MinRangeLaser'); lmax=sc.get('I_MaxRangeLaser')
        lcur=sc.get('I_Spessore_mm')
        lv=sc.get('AppValidValue',sc.get('O_LaserInRange'))
        self._pv_lmin.set(f"{lmin:.2f} mm" if lmin is not None else "—")
        self._pv_lmax.set(f"{lmax:.2f} mm" if lmax is not None else "—")
        self._pv_lcur.set(f"{lcur:.2f} mm" if lcur is not None else "—")
        if lv is None:
            self._pv_lval.set("—"); self._lbl_lval.config(fg=MUTED_CLR)
        elif lv:
            self._pv_lval.set("✓ IN RANGE"); self._lbl_lval.config(fg=OK_CLR)
        else:
            self._pv_lval.set("✗ FUORI RANGE"); self._lbl_lval.config(fg=ERR_CLR)

        # Pinza
        self._pv_piece.set("SÌ" if sc.get('I_PiecePresence',False) else "NO")
        self._pv_zc.set("SÌ" if sc.get('Pinza.InZonaControllo',False) else "NO")
        self._pv_zr.set("SÌ" if sc.get('Pinza.InZonaRallenta',False) else "NO")
        pos=sc.get('I_ActPosition'); spd=sc.get('I_ActSpeed')
        dl=int(sc.get('I_DirLavoro',2))
        self._pv_pos.set(f"{pos:.2f}" if pos is not None else "—")
        self._pv_spd.set(f"{spd:.2f}" if spd is not None else "—")
        self._pv_dir.set({0:"0=entrambe",1:"1=positive",2:"2=negative"}.get(dl,str(dl)))


# ══════════════════════════════════════════════════════════════════
if __name__=="__main__":
    app=ThicknessApp()
    app.mainloop()
