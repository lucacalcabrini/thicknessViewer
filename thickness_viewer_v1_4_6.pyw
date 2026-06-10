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

APP_VERSION = "1.4.18"
APP_BUILD   = "2026-06-10"
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
_RESTARTING = False   # True durante un auto-update: NON uccidere l'albero dei figli
                      # (il nuovo exe è un figlio che deve sopravvivere alla chiusura)

def _kill_tree():
    if os.getpid() != _MAIN_PID: return
    try:
        if sys.platform == "win32":
            import subprocess
            # In update niente /T: altrimenti taskkill ucciderebbe anche il nuovo exe
            cmd = (["taskkill","/F","/PID",str(_MAIN_PID)] if _RESTARTING
                   else ["taskkill","/F","/T","/PID",str(_MAIN_PID)])
            subprocess.call(cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)
        else:
            if _RESTARTING:
                os.kill(_MAIN_PID, signal.SIGKILL)
            else:
                os.killpg(os.getpgid(_MAIN_PID), signal.SIGKILL)
    except Exception:
        try: os.kill(_MAIN_PID, signal.SIGKILL)
        except Exception: pass

atexit.register(_kill_tree)


def _pid_alive(pid):
    """True se il processo <pid> è ancora in esecuzione (Windows)."""
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
            capture_output=True, text=True,
            creationflags=0x08000000).stdout or ""
        return str(int(pid)) in out
    except Exception:
        return False


def _apply_update_and_relaunch(target, old_pid):
    """Eseguito dal NUOVO exe in staging (file .upd):
    1) attende la chiusura del vecchio processo;
    2) si copia SOPRA <target> (stesso nome e posizione → l'icona non si sposta);
    3) riavvia l'exe aggiornato.
    Esce con os._exit per NON far scattare _kill_tree sul figlio appena lanciato."""
    import time as _t, shutil, subprocess
    staging = os.path.abspath(sys.executable)
    target  = os.path.abspath(target)
    # 1) attende che il vecchio termini (max ~15s)
    for _ in range(150):
        if old_pid <= 0 or not _pid_alive(old_pid):
            break
        _t.sleep(0.1)
    # 2) sovrascrive il vecchio exe col nuovo, ritentando se ancora bloccato (~12s)
    copied = False
    for _ in range(60):
        try:
            shutil.copy2(staging, target); copied = True; break
        except Exception:
            _t.sleep(0.2)
    # 3) riavvia l'exe (nome/posizione stabili); fallback prudente se la copia fallisce
    launch = target if (copied or os.path.isfile(target)) else staging
    try:
        DET = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        NPG = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        subprocess.Popen([launch], creationflags=DET | NPG)
    except Exception:
        try: subprocess.Popen([launch])
        except Exception: pass
    os._exit(0)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re, datetime, time, struct, sqlite3, configparser

import matplotlib; matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.ticker import MultipleLocator
import numpy as np


class _MiniToolbar(NavigationToolbar2Tk):
    """Toolbar matplotlib ridotta ai soli pulsanti utili.
    Rimuove Back/Forward (che apparivano come quadrati grigi disabilitati),
    il pulsante Subplots e i separatori grigi."""
    toolitems = (
        ('Home', 'Ripristina la vista iniziale', 'home', 'home'),
        ('Pan',  'Sposta il grafico (trascina)', 'move', 'pan'),
        ('Zoom', 'Zoom su area (rettangolo)',     'zoom_to_rect', 'zoom'),
        ('Save', 'Salva il grafico come immagine', 'filesave', 'save_figure'),
    )

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

PROGRAMDATA_INI = r"C:\ProgramData\ThicknessViewer\thickness_viewer.ini"
PROGRAMDATA_RESUME = r"C:\ProgramData\ThicknessViewer\resume_state.json"

def load_settings():
    cfg = configparser.ConfigParser()
    cfg['PLC'] = {'ip':'192.168.0.1','rack':'0','slot':'1','db':'16070'}
    cfg['SQL'] = {'path':'thickness_archive.sqlite'}
    cfg['UPDATE'] = {'auto_update':'false','check_interval_min':'5'}
    if not os.path.isfile(PROGRAMDATA_INI):
        try:
            os.makedirs(os.path.dirname(PROGRAMDATA_INI), exist_ok=True)
            with open(PROGRAMDATA_INI, 'w', encoding='utf-8') as f: cfg.write(f)
        except Exception: pass
    else:
        try: cfg.read(PROGRAMDATA_INI, encoding='utf-8')
        except Exception: pass
    return cfg, PROGRAMDATA_INI

def save_settings(cfg):
    try:
        os.makedirs(os.path.dirname(PROGRAMDATA_INI), exist_ok=True)
        with open(PROGRAMDATA_INI, 'w', encoding='utf-8') as f: cfg.write(f)
        return True
    except Exception: return False

def resolve_sql(raw):
    return raw if os.path.isabs(raw) else os.path.join(get_app_dir(), raw)


# ══════════════════════════════════════════════════════════════════
#  RESUME STATE
# ══════════════════════════════════════════════════════════════════

def load_resume_state():
    try:
        if os.path.isfile(PROGRAMDATA_RESUME):
            import json
            with open(PROGRAMDATA_RESUME, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None

def save_resume_state(state):
    try:
        import json
        os.makedirs(os.path.dirname(PROGRAMDATA_RESUME), exist_ok=True)
        with open(PROGRAMDATA_RESUME, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        return True
    except Exception:
        return False

def delete_resume_state():
    try:
        if os.path.isfile(PROGRAMDATA_RESUME):
            os.remove(PROGRAMDATA_RESUME)
    except Exception:
        pass


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
THRESHOLD_CLR="#e3b341"
AUTOEXP_CLR="#56d364";   TAR_CLR="#d2a8ff";       LASER_CLR="#4fc3f7"
MEAN_CLR="#a371f7"   # linea/banda spessore medio
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
        # Icona finestra: usa l'ico embedded nell'exe (frozen) o il file locale
        try:
            if getattr(sys, 'frozen', False):
                self.iconbitmap(sys.executable)
            else:
                _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')
                if os.path.isfile(_ico):
                    self.iconbitmap(_ico)
        except Exception:
            pass

        self._cfg, self._cfg_path = load_settings()
        self.db_data = None
        self._ae_running = False
        self._ae_timer   = None
        self._ae_client  = None
        self._ae_cok = self._ae_cnok = self._ae_ctar = 0
        self._ae_sql_con = None
        self._ae_prev    = None
        self._update_check_timer = None
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
        self.after(100, self._cleanup_update_leftovers)
        self.after(800, self._check_and_apply_resume)
        self.after(2000, self._schedule_update_check)

    # ── AUTO-UPDATE ───────────────────────────────────────────
    _GITHUB_REPO = "lucacalcabrini/thicknessViewer"

    def _check_for_updates(self):
        """Avvia controllo aggiornamenti in background (timeout 5s).
        Se non trova connessione entro 5s, prosegue senza bloccare."""
        import threading
        threading.Thread(target=self._update_thread, daemon=True).start()

    def _update_thread(self):
        import urllib.request, json
        try:
            url = f"https://api.github.com/repos/{self._GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ThicknessProfiler-AutoUpdater",
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())

            latest_tag = data.get("tag_name", "").lstrip("v")

            def _ver(v):
                try:    return tuple(int(x) for x in v.split("."))
                except: return (0,)

            if _ver(latest_tag) <= _ver(APP_VERSION):
                return  # già aggiornato o versione sconosciuta

            exe_asset = next(
                (a for a in data.get("assets", []) if a["name"].endswith(".exe")),
                None
            )
            if not exe_asset:
                return

            self.after(0, lambda: self._offer_update(
                latest_tag,
                exe_asset["browser_download_url"],
                exe_asset["name"],
                exe_asset.get("size", 0),
            ))
        except Exception:
            pass  # nessuna connessione o errore → prosegue normalmente

    def _offer_update(self, new_ver, url, filename, size_bytes):
        import threading
        size_mb = size_bytes / 1024 / 1024
        self.app_log(
            f"⬇  v{new_ver} disponibile ({size_mb:.1f} MB) — download in corso…")
        threading.Thread(
            target=self._download_and_restart,
            args=(url, filename),
            daemon=True
        ).start()

    def _cleanup_update_leftovers(self):
        """All'avvio rimuove i file temporanei di update (.upd) e i vecchi
        ThicknessProfiler_v*.exe diversi da quello attualmente in esecuzione."""
        import glob
        if not getattr(sys, 'frozen', False):
            return
        exe_dir  = os.path.dirname(os.path.abspath(sys.executable))
        exe_name = os.path.basename(sys.executable)
        leftovers = (glob.glob(os.path.join(exe_dir, "ThicknessProfiler_v*.exe"))
                     + glob.glob(os.path.join(exe_dir, "*.upd")))
        for old in leftovers:
            if os.path.basename(old) != exe_name:
                try: os.remove(old)
                except Exception: pass

    def _download_and_restart(self, url, filename):
        """Scarica il nuovo exe in un file di staging «<exe>.upd» nella stessa
        cartella, poi lo lancia in modalità --apply-update: il nuovo exe si copia
        SOPRA quello in esecuzione (stesso nome e posizione → l'icona non si sposta)
        e lo riavvia da solo."""
        global _RESTARTING
        import urllib.request, subprocess
        if not getattr(sys, 'frozen', False):
            self.after(0, lambda: messagebox.showinfo(
                "Info",
                "Auto-update disponibile solo nell'exe compilato.\n"
                "Scarica manualmente la nuova versione da GitHub.",
                parent=self))
            return

        exe_path = os.path.abspath(sys.executable)
        staging  = exe_path + ".upd"      # stesso percorso, suffisso temporaneo

        try:
            self.after(0, lambda: self.title("⬇  Connessione…"))
            req = urllib.request.Request(
                url, headers={"User-Agent": "ThicknessProfiler-AutoUpdater"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done  = 0
                with open(staging, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            self.after(0, lambda p=pct:
                                self.title(f"⬇  Download {p}%…"))
        except Exception as e:
            if os.path.isfile(staging):
                try: os.remove(staging)
                except Exception: pass
            self.after(0, lambda err=str(e): messagebox.showerror(
                "Errore aggiornamento",
                f"Download fallito:\n{err}\n\n"
                f"Scarica manualmente da:\n"
                f"https://github.com/{self._GITHUB_REPO}/releases/latest",
                parent=self))
            self.after(0, lambda: self.title(
                f"◈ Thickness Profiler  {APP_RELEASE}  —  {FB_TARGET}"))
            return

        # Lancia il nuovo exe (staging) che si copierà sopra exe_path e riavvierà
        try:
            _RESTARTING = True   # impedisce a _kill_tree (/T) di uccidere il figlio
            save_resume_state(self._collect_resume_state())
            subprocess.Popen(
                [staging, "--apply-update", exe_path, str(os.getpid())],
                creationflags=subprocess.DETACHED_PROCESS
                              | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            self.after(0, self.destroy)
        except Exception as e:
            _RESTARTING = False
            delete_resume_state()
            if os.path.isfile(staging):
                try: os.remove(staging)
                except Exception: pass
            self.after(0, lambda err=str(e): messagebox.showerror(
                "Errore aggiornamento",
                f"Impossibile avviare la nuova versione:\n{err}",
                parent=self))

    # ── Periodic update check ────────────────────────────────
    def _schedule_update_check(self):
        if not self._cfg.getboolean('UPDATE', 'auto_update', fallback=False):
            return
        self._check_for_updates()
        try:
            mins = max(1, int(self._cfg.get('UPDATE', 'check_interval_min', fallback='5')))
        except (ValueError, TypeError):
            mins = 5
        self._update_check_timer = self.after(mins * 60 * 1000, self._schedule_update_check)

    # ── Resume state ─────────────────────────────────────────
    def _collect_resume_state(self):
        slots = []
        for slot in getattr(self, '_ae_db_slots', []):
            slots.append({
                'enabled': slot['enabled'].get(),
                'db_num':  slot['db_num'].get().strip()
            })
        return {
            'autoexport_running': bool(getattr(self, '_ae_running', False)),
            'plc_ip':   self._pv_ip.get().strip()   if hasattr(self, '_pv_ip')   else self._cfg['PLC'].get('ip',   ''),
            'plc_rack': self._pv_rack.get()          if hasattr(self, '_pv_rack') else self._cfg['PLC'].get('rack', '0'),
            'plc_slot': self._pv_slot.get()          if hasattr(self, '_pv_slot') else self._cfg['PLC'].get('slot', '1'),
            'poll_ms':  int(self._pv_poll.get() or 50)        if hasattr(self, '_pv_poll')       else 50,
            'archive_tarature': self._pv_ae_tar.get()         if hasattr(self, '_pv_ae_tar')     else True,
            'viewer_slot':      self._ae_viewer_var.get()     if hasattr(self, '_ae_viewer_var') else 0,
            'slots': slots,
        }

    def _check_and_apply_resume(self):
        state = load_resume_state()
        if state is None:
            return
        delete_resume_state()
        self._apply_resume_state(state)

    def _apply_resume_state(self, state):
        try:
            if hasattr(self, '_pv_ip')   and 'plc_ip'   in state: self._pv_ip.set(state['plc_ip'])
            if hasattr(self, '_pv_rack') and 'plc_rack' in state: self._pv_rack.set(str(state['plc_rack']))
            if hasattr(self, '_pv_slot') and 'plc_slot' in state: self._pv_slot.set(str(state['plc_slot']))
        except Exception:
            pass
        try:
            if hasattr(self, '_pv_poll'):       self._pv_poll.set(str(state.get('poll_ms', 50)))
            if hasattr(self, '_pv_ae_tar'):     self._pv_ae_tar.set(state.get('archive_tarature', True))
            if hasattr(self, '_ae_viewer_var'): self._ae_viewer_var.set(state.get('viewer_slot', 0))
            for i, sd in enumerate(state.get('slots', [])):
                if i < len(self._ae_db_slots):
                    self._ae_db_slots[i]['enabled'].set(sd.get('enabled', False))
                    self._ae_db_slots[i]['db_num'].set(sd.get('db_num', ''))
        except Exception:
            pass
        if state.get('autoexport_running', False):
            self.app_log("▶ Ripristino Auto-Export dopo aggiornamento…")
            self.after(500, self._ae_start)
        else:
            self.app_log("✓ Configurazione ripristinata dopo aggiornamento")

    # ── Settings ─────────────────────────────────────────────
    def _save_ini(self):
        try: save_settings(self._cfg)
        except Exception: pass

    def _startup(self):
        miss=[m for m in ['numpy','matplotlib']
              if not __import__('importlib').util.find_spec(m)]
        if miss:
            messagebox.showwarning("Librerie mancanti",
                f"pip install {' '.join(miss)}")

    def _on_close(self):
        if self._ae_running: self._ae_stop()
        if self._update_check_timer:
            try: self.after_cancel(self._update_check_timer)
            except Exception: pass
            self._update_check_timer = None
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
        # Pulsante pausa aggiornamento viewer
        self._viewer_paused = False
        self._btn_pause=tk.Button(top,text="⏸ Viewer LIVE",
            font=("Consolas",9,"bold"),bg=ACCENT,fg=DARK_BG,
            relief="flat",padx=6,command=self._toggle_viewer_pause)
        self._btn_pause.pack(side="left",padx=4)
        ttk.Button(top,text="⚙ Impostazioni",
                   command=self._open_settings).pack(side="right",padx=2)

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

        # fine pannello sinistro

    # ── Pannello destro ───────────────────────────────────────
    def _build_right(self, P):
        self._nb=ttk.Notebook(P); self._nb.pack(fill="both",expand=True)
        for title,builder in [
                ("  📊 Profilo  ",  self._tab_profilo),
                ("  📈 Delta  ",    self._tab_delta),
                ("  🔌 PLC Reader  ",self._tab_plc),
                ("  ⚡ Auto-Export  ",self._tab_autoexp),
                ("  📚 History  ",  self._tab_history)]:
            t=ttk.Frame(self._nb); self._nb.add(t,text=title); builder(t)

    # ══════════════════════════════════════════════════════════
    #  TAB 1 — PROFILO  (visualizzazione centrata sul delta)
    # ══════════════════════════════════════════════════════════
    def _tab_profilo(self, P):
        bar=ttk.Frame(P); bar.pack(fill="x",padx=4,pady=4)
        self._ck_prof=tk.BooleanVar(value=True)
        self._ck_base=tk.BooleanVar(value=False)  # baseline nascosta di default
        self._ck_thresh=tk.BooleanVar(value=True)
        self._ck_nok=tk.BooleanVar(value=True)
        self._ck_mean=tk.BooleanVar(value=True)        # linea spessore medio (tutte le celle)
        self._ck_std=tk.BooleanVar(value=False)        # banda +/- dev.std
        self._ck_stats=tk.BooleanVar(value=True)       # box statistiche
        self._ck_clean_mean=tk.BooleanVar(value=True)  # media celle entro soglia (esclusi NOK)
        for var,txt in [(self._ck_prof,"Profilo spessore"),
                        (self._ck_base,"Baseline (quota supporto)"),
                        (self._ck_thresh,"Soglie"),
                        (self._ck_nok,"Celle NOK"),
                        (self._ck_mean,"Spessore medio"),
                        (self._ck_std,"Banda ±σ"),
                        (self._ck_stats,"Statistiche"),
                        (self._ck_clean_mean,"Media celle OK")]:
            tk.Checkbutton(bar,text=txt,variable=var,bg=DARK_BG,fg=TEXT_CLR,
                selectcolor="#1f6feb",activebackground=DARK_BG,font=("Consolas",9),
                command=self._draw_profilo).pack(side="left",padx=4)
        # ── Riga 2: controllo passo griglia ───────────────────
        bar2=ttk.Frame(P); bar2.pack(fill="x",padx=4,pady=(0,2))
        tk.Label(bar2,text="Griglia:",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8,"bold")).pack(side="left",padx=(4,6))
        tk.Label(bar2,text="Y [mm]:",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8)).pack(side="left")
        self._pv_grid_y=tk.StringVar(value="0.10")
        cb_y=ttk.Combobox(bar2,textvariable=self._pv_grid_y,
                          values=["0.10","0.25","0.50","1.00"],
                          width=5,state="readonly",font=("Consolas",8))
        cb_y.pack(side="left",padx=(2,12))
        tk.Label(bar2,text="X [mm]:",bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8)).pack(side="left")
        self._pv_grid_x=tk.StringVar(value="10")
        cb_x=ttk.Combobox(bar2,textvariable=self._pv_grid_x,
                          values=["5","10","20","50"],
                          width=5,state="readonly",font=("Consolas",8))
        cb_x.pack(side="left",padx=2)
        cb_y.bind("<<ComboboxSelected>>",lambda _:self._draw_profilo())
        cb_x.bind("<<ComboboxSelected>>",lambda _:self._draw_profilo())

        self.fig_p=Figure(figsize=(10,6),dpi=95,facecolor=DARK_BG)
        self.ax_p=self.fig_p.add_subplot(111,facecolor=PANEL_BG)
        self._sax(self.ax_p)
        cv=FigureCanvasTkAgg(self.fig_p,P)
        cv.get_tk_widget().pack(fill="both",expand=True,padx=4,pady=4)
        self._cv_p=cv

        # ── Barra strumenti matplotlib ridotta (Home / Pan / Zoom / Salva) ──
        _TB="#1c2128"; _BTN="#2d333b"
        bot=tk.Frame(P,bg=_TB); bot.pack(fill="x",padx=4,pady=(0,3))
        tk.Label(bot,text="🛠 Home  ✋Pan  🔍Zoom  💾Salva →",bg=_TB,fg=MUTED_CLR,
                 font=("Consolas",8,"bold")).pack(side="left",padx=(4,8))
        tb=_MiniToolbar(cv,bot)
        tb.config(background=_TB)
        for _ch in tb.winfo_children():
            cls=_ch.winfo_class()
            if cls in ("Button","Checkbutton"):      # Pan/Zoom sono Checkbutton (toggle)
                try:
                    _ch.config(bg=_BTN,activebackground=ACCENT,
                               relief="raised",bd=1,padx=3,pady=2)
                except Exception: pass
                try: _ch.config(selectcolor=ACCENT)  # evidenzia il tool attivo
                except Exception: pass
            else:                                     # label coordinate, ecc.
                try: _ch.config(bg=_TB)
                except Exception: pass
                try: _ch.config(fg=MUTED_CLR)
                except Exception: pass
        tb.update()

    def _sax(self, ax):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=TEXT_CLR,which='both')
        for sp in ax.spines.values(): sp.set_color(BORDER_CLR)
        ax.grid(True,alpha=0.15,color=BORDER_CLR)

    def _draw_all(self): self._draw_profilo(); self._draw_delta()

    def _profile_arrays(self, ar, n=ARRAY_SIZE):
        prof = np.array(ar.get('aProfiloSpessore', [])[:n], dtype=float) if len(ar.get('aProfiloSpessore', [])) >= n else None
        dlt  = np.array(ar.get('aProfiloDelta', [])[:n], dtype=float) if len(ar.get('aProfiloDelta', [])) >= n else None
        bas  = np.array(ar.get('aBaseline', [])[:n], dtype=float) if len(ar.get('aBaseline', [])) >= n else None
        nraw = np.array(ar.get('aNraw', [])[:n], dtype=float) if len(ar.get('aNraw', [])) >= n else None

        mask = np.zeros(n, dtype=bool)
        if nraw is not None:
            # Con PLC v1.2+: aNraw viene azzerata a ogni Fp.Q, quindi nraw > 0
            # identifica ESATTAMENTE le celle campionate nella passata corrente,
            # indipendentemente dalla loro posizione nell'array.
            mask |= nraw > 0
        else:
            # Fallback per dati da file .db (senza aNraw): usa prof != 0.
            # Con PLC v1.2+ aProfiloSpessore è anch'essa azzerata al Fp.Q,
            # quindi anche questo fallback produce il risultato corretto.
            if prof is not None:
                mask |= np.isfinite(prof) & (np.abs(prof) > 1e-6)
            if dlt is not None:
                mask |= np.isfinite(dlt) & (np.abs(dlt) > 1e-6)

        if prof is not None:
            mask &= np.isfinite(prof) & (prof > -100.0) & (prof < 1000.0)
        if dlt is not None:
            mask &= np.isfinite(dlt) & (np.abs(dlt) < 1000.0)
        return prof, dlt, bas, nraw, mask

    def _draw_profilo(self):
        # Ricostruzione completa: clf() + nuovo subplot garantisce slate pulita
        self.fig_p.clf()
        self.ax_p = self.fig_p.add_subplot(111, facecolor=PANEL_BG)
        self._sax(self.ax_p)
        ax = self.ax_p
        if self.db_data is None:
            ax.text(0.5,0.5,"Nessun dato - leggi dal PLC o apri una riga History",
                    ha='center',va='center',color=MUTED_CLR,fontsize=12,
                    transform=ax.transAxes)
            self._cv_p.draw_idle(); return

        sc=self.db_data.get('scalars',{}); ar=self.db_data.get('arrays',{})
        rc  = self._gs(sc,'I_ParametriCntrolloSpessore.RangeControllo','RangeControllo',default=80.0)
        pc  = self._gs(sc,'I_ParametriCntrolloSpessore.PosizioneCentroVentosa','PosizioneCentroVentosa',default=1930.0)
        sg  = self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreMassimo','SpessoreMassimo',default=1.0)
        sp_att = self._gs(sc,'I_SpessoreAtteso',default=2.98)
        dl  = int(self._gs(sc,'I_DirLavoro',default=2))

        n=ARRAY_SIZE
        x=np.linspace(pc-rc, pc+rc, n)
        prof_arr, dlt_arr, bas_arr, nraw_arr, mask_valid = self._profile_arrays(ar, n)
        plotted=False

        if bas_arr is not None and self._ck_base.get():
            mask_base=np.isfinite(bas_arr) & (bas_arr > 1e-6)
            if mask_base.any():
                ax_base=ax.twinx()
                ax_base.set_facecolor('none')
                ax_base.tick_params(colors=BASELINE_CLR,which='both')
                ax_base.spines['right'].set_color(BASELINE_CLR)
                ax_base.plot(x[mask_base],bas_arr[mask_base],color=BASELINE_CLR,lw=0.9,ls=':',
                             alpha=0.55,label='Baseline quota supporto [mm]')
                ax_base.set_ylabel('Baseline laser [mm]',color=BASELINE_CLR,fontsize=9)
                plotted=True

        if prof_arr is not None and self._ck_prof.get():
            mask_prof=mask_valid & np.isfinite(prof_arr)
            if mask_prof.any():
                ax.plot(x[mask_prof],prof_arr[mask_prof],color=PROFILE_CLR,lw=2.0,
                        label='Spessore reale = baseline - laser [mm]',zorder=5)
                ax.scatter(x[mask_prof],prof_arr[mask_prof],color=PROFILE_CLR,s=10,
                           alpha=0.45,zorder=6)
                plotted=True

        if self._ck_thresh.get():
            ax.axhline(sp_att,color=OK_CLR,lw=1.4,ls='-',alpha=0.85,
                       label=f'Spessore atteso: {sp_att:.3f} mm')
            ax.axhline(sp_att+sg,color=THRESHOLD_CLR,lw=1.3,ls='--',alpha=0.95,
                       label=f'Soglia NOK: {sp_att+sg:.3f} mm')
            ax.axhspan(max(0,sp_att-sg),sp_att+sg,alpha=0.055,color=OK_CLR,zorder=1)
            ax.axhspan(sp_att+sg,sp_att+max(sg*3,0.2),alpha=0.07,color=ERR_CLR,zorder=1)
            plotted=True

        if self._ck_nok.get() and prof_arr is not None and dlt_arr is not None:
            mask_delta=mask_valid & np.isfinite(dlt_arr)
            mask_nok=mask_delta & (dlt_arr > sg)
            mask_warn=mask_delta & (dlt_arr > sg*0.70) & (dlt_arr <= sg)
            if mask_warn.any():
                ax.scatter(x[mask_warn],prof_arr[mask_warn],color=WARN_CLR,s=26,
                           marker='o',edgecolors=DARK_BG,linewidths=0.4,
                           label=f'Vicino soglia ({int(mask_warn.sum())})',zorder=8)
            if mask_nok.any():
                ax.scatter(x[mask_nok],prof_arr[mask_nok],color=FUORI_CLR,s=42,
                           marker='x',lw=2.0,
                           label=f'Celle NOK Delta>{sg:.2f}mm ({int(mask_nok.sum())})',zorder=9)
                ax.vlines(x[mask_nok],sp_att+sg,prof_arr[mask_nok],
                          colors=FUORI_CLR,lw=0.8,alpha=0.45,zorder=7)
                plotted=True

        # ── Spessore medio / dispersione / statistiche ────────
        prof_valid = (prof_arr[mask_valid & np.isfinite(prof_arr)]
                      if prof_arr is not None else np.array([]))
        if prof_valid.size:
            mean_v = float(np.mean(prof_valid))
            std_v  = float(np.std(prof_valid))
            min_v  = float(np.min(prof_valid))
            max_v  = float(np.max(prof_valid))
            if dlt_arr is not None:
                dvalid = dlt_arr[mask_valid & np.isfinite(dlt_arr)]
                n_nok  = int(np.count_nonzero(dvalid > sg))
                n_warn = int(np.count_nonzero((dvalid > sg*0.70) & (dvalid <= sg)))
                # celle entro soglia (escluse NOK)
                mask_ok = mask_valid & np.isfinite(prof_arr) & np.isfinite(dlt_arr) & (dlt_arr <= sg)
                prof_ok  = prof_arr[mask_ok]
                clean_mean_v = float(np.mean(prof_ok)) if prof_ok.size else None
            else:
                n_nok = n_warn = 0; clean_mean_v = None; prof_ok = np.array([])
            if self._ck_std.get():
                ax.axhspan(mean_v-std_v, mean_v+std_v, alpha=0.10,
                           color=MEAN_CLR, zorder=2,
                           label=f'Banda ±σ = {std_v:.3f} mm')
            if self._ck_mean.get():
                ax.axhline(mean_v, color=MEAN_CLR, lw=1.7, ls='-.', alpha=0.95,
                           label=f'Spessore medio (tutte): {mean_v:.3f} mm', zorder=6)
            if self._ck_clean_mean.get() and clean_mean_v is not None:
                ax.axhline(clean_mean_v, color=OK_CLR, lw=1.8, ls='--', alpha=0.92,
                           label=f'Media celle OK ({prof_ok.size}c): {clean_mean_v:.3f} mm',
                           zorder=7)
            if self._ck_stats.get():
                cm_line = (f"med.ok  {clean_mean_v:7.3f} mm\n"
                           if clean_mean_v is not None and self._ck_clean_mean.get() else "")
                stats_txt=("STATISTICHE\n"
                           f"media   {mean_v:7.3f} mm\n"
                           + cm_line +
                           f"min     {min_v:7.3f} mm\n"
                           f"max     {max_v:7.3f} mm\n"
                           f"dev.std {std_v:7.3f} mm\n"
                           f"celle   {prof_valid.size:>4d}/{n}\n"
                           f"vicino  {n_warn:>4d}\n"
                           f"NOK     {n_nok:>4d}")
                ax.text(0.013,0.97,stats_txt,transform=ax.transAxes,
                        ha='left',va='top',fontsize=8,family='monospace',
                        color=TEXT_CLR,zorder=12,
                        bbox=dict(boxstyle='round,pad=0.45',facecolor=PANEL_BG,
                                  edgecolor=MEAN_CLR,alpha=0.93))
            plotted=True

        if not plotted or (prof_arr is None and dlt_arr is None):
            ax.text(0.5,0.5,"Dati profilo non presenti nel DB letto",
                    ha='center',va='center',color=WARN_CLR,fontsize=12,
                    transform=ax.transAxes)
        elif prof_arr is not None and not mask_valid.any():
            ax.text(0.5,0.5,"Profilo presente ma nessuna cella valida da disegnare",
                    ha='center',va='center',color=WARN_CLR,fontsize=12,
                    transform=ax.transAxes)

        ax.set_xlabel("Posizione asse [mm]", color=TEXT_CLR, fontsize=10)
        ax.set_ylabel("Spessore disco [mm]", color=TEXT_CLR, fontsize=10)

        nf=int(sc.get('AppNcelleFuoriSoglia',sc.get('O_nCelleFuoriSoglia',0)))
        ns=int(self._gs(sc,'I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme',default=15))
        dls={0:"entrambe",1:"positive",2:"negative"}.get(dl,str(dl))
        fn=self.db_data.get('filename','-')
        nok_flag=bool(sc.get('AppSpessoreNok',sc.get('O_SpessoreNOK',False)))
        esito="NOK" if nok_flag else "OK"
        valid_count=int(mask_valid.sum())
        ax.set_title(f"{fn}   {esito}   celle profilo: {valid_count}/{n}   fuori: {nf}/{ns}   dir: {dls}",
                     color=ERR_CLR if nok_flag else OK_CLR,fontsize=9,pad=6)

        ax.axvline(pc-rc,color=BORDER_CLR,lw=0.7,ls=':',alpha=0.5)
        ax.axvline(pc+rc,color=BORDER_CLR,lw=0.7,ls=':',alpha=0.5)
        ax.axvline(pc,color=BORDER_CLR,lw=0.4,ls='--',alpha=0.3)

        leg=ax.legend(loc='upper right',fontsize=8,framealpha=0.9,
                      facecolor=PANEL_BG,edgecolor=BORDER_CLR,labelcolor=TEXT_CLR)
        if leg: leg.get_frame().set_facecolor(PANEL_BG)

        # ── Griglia a passo configurabile ─────────────────────
        try:
            ax.yaxis.set_major_locator(MultipleLocator(float(self._pv_grid_y.get())))
            ax.xaxis.set_major_locator(MultipleLocator(float(self._pv_grid_x.get())))
        except Exception:
            pass

        try: self.fig_p.tight_layout()
        except Exception: pass
        self._cv_p.draw()
        self._cv_p.get_tk_widget().update_idletasks()

    @staticmethod
    def _gs(sc,*keys,default=0.0):
        for k in keys:
            if k in sc:
                try: return float(sc[k])
                except: pass
        return default

    # ══════════════════════════════════════════════════════════
    #  TAB 2 — DELTA (deviazione dallo spessore atteso)
    # ══════════════════════════════════════════════════════════
    def _tab_delta(self, P):
        bar=ttk.Frame(P); bar.pack(fill="x",padx=4,pady=4)
        ttk.Label(bar,
            text="Δ = spessore misurato − spessore atteso   •   verde=OK  rosso=NOK",
            style="Muted.TLabel").pack(side="left")
        self._ck_dmean=tk.BooleanVar(value=True)    # linea delta medio
        self._ck_dstats=tk.BooleanVar(value=True)   # box statistiche delta
        for var,txt in [(self._ck_dmean,"Delta medio"),
                        (self._ck_dstats,"Statistiche")]:
            tk.Checkbutton(bar,text=txt,variable=var,bg=DARK_BG,fg=TEXT_CLR,
                selectcolor="#1f6feb",activebackground=DARK_BG,font=("Consolas",9),
                command=self._draw_delta).pack(side="left",padx=8)
        self.fig_d=Figure(figsize=(10,6),dpi=95,facecolor=DARK_BG)
        self.ax_d=self.fig_d.add_subplot(111,facecolor=PANEL_BG)
        self._sax(self.ax_d)
        cv=FigureCanvasTkAgg(self.fig_d,P)
        cv.get_tk_widget().pack(fill="both",expand=True,padx=4,pady=4)
        self._cv_d=cv

    def _draw_delta(self):
        self.fig_d.clf()
        self.ax_d = self.fig_d.add_subplot(111, facecolor=PANEL_BG)
        self._sax(self.ax_d)
        ax = self.ax_d
        if self.db_data is None:
            ax.text(0.5,0.5,"Nessun dato",ha='center',va='center',
                    color=MUTED_CLR,fontsize=12,transform=ax.transAxes)
            self._cv_d.draw_idle(); return
        sc=self.db_data.get('scalars',{}); ar=self.db_data.get('arrays',{})
        rc  = self._gs(sc,'I_ParametriCntrolloSpessore.RangeControllo','RangeControllo',default=80.0)
        pc  = self._gs(sc,'I_ParametriCntrolloSpessore.PosizioneCentroVentosa','PosizioneCentroVentosa',default=1930.0)
        sg  = self._gs(sc,'I_ParametriCntrolloSpessore.SpessoreMassimo','SpessoreMassimo',default=1.0)
        sp_att = self._gs(sc,'I_SpessoreAtteso',default=2.98)
        n=ARRAY_SIZE; x=np.linspace(pc-rc,pc+rc,n)
        prof_arr, dlt_arr, bas_arr, nraw_arr, mask_valid = self._profile_arrays(ar, n)
        if dlt_arr is not None:
            mask=mask_valid & np.isfinite(dlt_arr)
            if mask.any():
                xm=x[mask]; dm=dlt_arr[mask]
                ax.fill_between(xm,0,dm,where=(dm>=0)&(dm<=sg),
                                color=OK_CLR,alpha=0.30,
                                label='Eccesso entro tolleranza')
                ax.fill_between(xm,sg,dm,where=(dm>sg),
                                color=ERR_CLR,alpha=0.55,
                                label='Eccesso oltre soglia')
                ax.fill_between(xm,dm,0,where=(dm<0),
                                color=ACCENT,alpha=0.22,
                                label='Difetto: disco piu sottile')
                ax.plot(xm,dm,color=DELTA_CLR,lw=1.7,zorder=5,
                        label='Delta profilo')
                ax.scatter(xm,dm,color=DELTA_CLR,s=10,alpha=0.45,zorder=6)

                mask_warn=mask & (dlt_arr > sg*0.70) & (dlt_arr <= sg)
                mask_nok=mask & (dlt_arr > sg)
                if mask_warn.any():
                    ax.scatter(x[mask_warn],dlt_arr[mask_warn],
                               color=WARN_CLR,s=28,marker='o',edgecolors=DARK_BG,
                               linewidths=0.4,label=f'Vicino soglia ({int(mask_warn.sum())})',zorder=8)
                if mask_nok.any():
                    ax.scatter(x[mask_nok],dlt_arr[mask_nok],
                               color=FUORI_CLR,s=44,marker='x',lw=2.0,
                               label=f'Celle NOK ({int(mask_nok.sum())})',zorder=9)

                dmean_v=float(np.mean(dm)); dstd_v=float(np.std(dm))
                dmin_v=float(np.min(dm));   dmax_v=float(np.max(dm))
                if self._ck_dmean.get():
                    ax.axhline(dmean_v,color=MEAN_CLR,lw=1.7,ls='-.',alpha=0.95,
                               label=f'Delta medio: {dmean_v:+.3f} mm',zorder=6)
                if self._ck_dstats.get():
                    stats_txt=("STATISTICHE Δ\n"
                               f"media   {dmean_v:+7.3f} mm\n"
                               f"min     {dmin_v:+7.3f} mm\n"
                               f"max     {dmax_v:+7.3f} mm\n"
                               f"dev.std {dstd_v:7.3f} mm\n"
                               f"celle   {dm.size:>4d}/{n}\n"
                               f"vicino  {int(mask_warn.sum()):>4d}\n"
                               f"NOK     {int(mask_nok.sum()):>4d}")
                    ax.text(0.013,0.97,stats_txt,transform=ax.transAxes,
                            ha='left',va='top',fontsize=8,family='monospace',
                            color=TEXT_CLR,zorder=12,
                            bbox=dict(boxstyle='round,pad=0.45',facecolor=PANEL_BG,
                                      edgecolor=MEAN_CLR,alpha=0.93))
            else:
                ax.text(0.5,0.5,"Delta presente ma nessuna cella valida da disegnare",
                        ha='center',va='center',color=WARN_CLR,fontsize=12,
                        transform=ax.transAxes)
        else:
            ax.text(0.5,0.5,"Array aProfiloDelta non presente nel DB letto",
                    ha='center',va='center',color=WARN_CLR,fontsize=12,
                    transform=ax.transAxes)

        ax.axhline(0,color=OK_CLR,lw=1.4,ls='-',alpha=0.85,
                   label=f'Delta=0  atteso {sp_att:.3f}mm')
        ax.axhline(sg,color=ERR_CLR,lw=1.5,ls='--',alpha=0.9,
                   label=f'Soglia NOK = {sg:.3f}mm')
        ax.axhline(-sg,color=BORDER_CLR,lw=0.9,ls=':',alpha=0.55)
        ax.axhspan(0,sg,alpha=0.045,color=OK_CLR,zorder=0)
        ax.axhspan(sg,sg*4,alpha=0.06,color=ERR_CLR,zorder=0)
        ax.axhspan(-sg,0,alpha=0.035,color=ACCENT,zorder=0)

        ax.set_xlabel("Posizione asse [mm]",color=TEXT_CLR,fontsize=10)
        ax.set_ylabel("Delta spessore (misurato - atteso) [mm]",color=TEXT_CLR,fontsize=10)

        nf=int(sc.get('AppNcelleFuoriSoglia',sc.get('O_nCelleFuoriSoglia',0)))
        ns=int(self._gs(sc,'I_ParametriCntrolloSpessore.nLettureConsecutiveAllarme',default=15))
        nok_flag=bool(sc.get('AppSpessoreNok',sc.get('O_SpessoreNOK',False)))
        esito="NOK" if nok_flag else "OK"
        valid_count=int(mask_valid.sum())
        ax.set_title(f"Profilo delta da baseline: {esito}   celle valide: {valid_count}/{n}   fuori: {nf}/{ns}",
                     color=ERR_CLR if nok_flag else OK_CLR,fontsize=10,pad=6)
        leg=ax.legend(loc='upper right',fontsize=8,framealpha=0.90,
                      facecolor=PANEL_BG,edgecolor=BORDER_CLR,labelcolor=TEXT_CLR)
        if leg: leg.get_frame().set_facecolor(PANEL_BG)
        try: self.fig_d.tight_layout()
        except Exception: pass
        self._cv_d.draw()
        self._cv_d.get_tk_widget().update_idletasks()
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
        lf_db=ttk.LabelFrame(left,text="  DB da monitorare  ",padding=6)
        lf_db.pack(fill="x",padx=6,pady=4)

        hdr=ttk.Frame(lf_db); hdr.pack(fill="x",pady=(0,2))
        tk.Label(hdr,text="On",bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",8),width=3).pack(side="left")
        tk.Label(hdr,text="DB #",bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",8),width=8).pack(side="left")
        tk.Label(hdr,text="→Viewer",bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",8),width=8).pack(side="left")
        tk.Label(hdr,text="Ultima passata",bg=DARK_BG,fg=MUTED_CLR,font=("Consolas",8)).pack(side="left",padx=(4,0))

        N_SLOTS = 10
        self._ae_db_slots = []
        self._ae_viewer_var = tk.IntVar(value=0)   # indice slot inviato al viewer

        defaults_db = ['16070', '17070'] + [''] * (N_SLOTS-2)

        for i, db_def in enumerate(defaults_db):
            row=ttk.Frame(lf_db); row.pack(fill="x",pady=1)
            slot={}
            slot['enabled']=tk.BooleanVar(value=(i in (0, 1)))
            tk.Checkbutton(row,variable=slot['enabled'],bg=DARK_BG,
                fg=OK_CLR,selectcolor="#1f6feb",activebackground=DARK_BG,
                width=2).pack(side="left")
            slot['db_num']=tk.StringVar(value=db_def)
            ttk.Entry(row,textvariable=slot['db_num'],width=8,
                      font=("Consolas",9)).pack(side="left",padx=2)
            tk.Radiobutton(row,variable=self._ae_viewer_var,value=i,
                bg=DARK_BG,fg=ACCENT,selectcolor=DARK_BG,
                activebackground=DARK_BG,width=6).pack(side="left",padx=2)
            slot['status']=tk.StringVar(value="—")
            tk.Label(row,textvariable=slot['status'],bg=DARK_BG,
                     fg=MUTED_CLR,font=("Consolas",8),anchor="w",
                     width=24).pack(side="left",padx=(2,0))
            slot['prev_counter'] = None
            slot['ok_count']  = 0
            slot['nok_count'] = 0
            slot['tar_count'] = 0
            self._ae_db_slots.append(slot)

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
        self._pv_ae_tar=tk.BooleanVar(value=True)
        tk.Checkbutton(lf_opt,text="Archivia anche le tarature",variable=self._pv_ae_tar,
            bg=DARK_BG,fg=TEXT_CLR,selectcolor="#1f6feb",activebackground=DARK_BG,
            font=("Consolas",9),anchor="w").pack(fill="x",pady=1)

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

    def _toggle_viewer_pause(self):
        self._viewer_paused = not self._viewer_paused
        if self._viewer_paused:
            self._btn_pause.config(text="▶ Viewer PAUSA", bg=WARN_CLR, fg=DARK_BG)
            self.app_log("Viewer in pausa — archivio SQLite continua")
        else:
            self._btn_pause.config(text="⏸ Viewer LIVE", bg=ACCENT, fg=DARK_BG)
            self.app_log("Viewer live")

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
            # Forza il repaint del canvas Tk prima di schedulare il prossimo poll.
            # draw() su TkAgg schedula il blit come idle → verrebbe soffocato dal
            # poll successivo. update_idletasks() lo scarica subito.
            self.update_idletasks()
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

        db_lbl = ""
        if slot:
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
        # Aggiorna viewer solo se questo slot è quello selezionato e non in pausa
        if not self._viewer_paused:
            viewer_idx = self._ae_viewer_var.get() if hasattr(self,'_ae_viewer_var') else 0
            try:
                slot_idx = self._ae_db_slots.index(slot) if slot else -1
            except (ValueError, AttributeError):
                slot_idx = -1
            if slot_idx == viewer_idx:
                # Passa dec direttamente: niente roundtrip gen_db_text/parse_db_text
                view_data = {**dec, 'filename': f"DB{db} #{rid} {prefix.strip()}"}
                self._load_data(view_data)

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
        wrap=ttk.Frame(P); wrap.pack(fill="both",expand=True)
        ttk.Frame(wrap).pack(expand=True)
        tk.Label(wrap,text="⚙",font=("Consolas",48),bg=DARK_BG,fg=MUTED_CLR).pack()
        ttk.Button(wrap,text="  Apri impostazioni  ",style="Accent.TButton",
                   command=self._open_settings).pack(pady=10)
        tk.Label(wrap,text=PROGRAMDATA_INI,bg=DARK_BG,fg=MUTED_CLR,
                 font=("Consolas",8)).pack()
        ttk.Frame(wrap).pack(expand=True)

    def _open_settings(self):
        dlg=tk.Toplevel(self)
        dlg.title("Impostazioni — Thickness Profiler")
        dlg.configure(bg=DARK_BG)
        dlg.geometry("460x470")
        dlg.resizable(False,False)
        dlg.grab_set(); dlg.transient(self)

        sv_ip  =tk.StringVar(value=self._cfg['PLC'].get('ip',  '192.168.0.1'))
        sv_rack=tk.StringVar(value=self._cfg['PLC'].get('rack','0'))
        sv_slot=tk.StringVar(value=self._cfg['PLC'].get('slot','1'))
        sv_db  =tk.StringVar(value=self._cfg['PLC'].get('db',  '16070'))
        sv_sql =tk.StringVar(value=self._cfg['SQL'].get('path','thickness_archive.sqlite'))

        def _row(parent, label, sv, width=22, tip=None):
            f=ttk.Frame(parent); f.pack(fill="x",pady=3)
            ttk.Label(f,text=label,style="Muted.TLabel",width=13).pack(side="left")
            ttk.Entry(f,textvariable=sv,width=width).pack(side="left",padx=4)
            if tip: ttk.Label(f,text=tip,style="Muted.TLabel",
                              font=("Consolas",8)).pack(side="left")

        plc_lf=ttk.LabelFrame(dlg,text="  Connessione PLC  ",padding=10)
        plc_lf.pack(fill="x",padx=14,pady=8)
        _row(plc_lf,"IP PLC:",sv_ip,tip="es. 192.168.0.1")
        r_rs=ttk.Frame(plc_lf); r_rs.pack(fill="x",pady=3)
        ttk.Label(r_rs,text="Rack:",style="Muted.TLabel",width=13).pack(side="left")
        ttk.Entry(r_rs,textvariable=sv_rack,width=5).pack(side="left",padx=4)
        ttk.Label(r_rs,text="Slot:",style="Muted.TLabel").pack(side="left",padx=(10,0))
        ttk.Entry(r_rs,textvariable=sv_slot,width=5).pack(side="left",padx=4)
        ttk.Label(r_rs,text="DB #:",style="Muted.TLabel").pack(side="left",padx=(10,0))
        ttk.Entry(r_rs,textvariable=sv_db,width=8).pack(side="left",padx=4)

        sql_lf=ttk.LabelFrame(dlg,text="  Database SQLite  ",padding=10)
        sql_lf.pack(fill="x",padx=14,pady=4)
        r_sql=ttk.Frame(sql_lf); r_sql.pack(fill="x")
        ttk.Label(r_sql,text="File SQLite:",style="Muted.TLabel",width=13).pack(side="left")
        ttk.Entry(r_sql,textvariable=sv_sql,width=28).pack(side="left",padx=4)
        def _browse():
            init=resolve_sql(sv_sql.get())
            fp=filedialog.asksaveasfilename(parent=dlg,title="SQLite",
                defaultextension=".sqlite",initialfile=os.path.basename(init),
                initialdir=os.path.dirname(init) or get_app_dir(),
                filetypes=[("SQLite","*.sqlite *.db3"),("Tutti","*.*")])
            if fp:
                try:
                    rel=os.path.relpath(fp,get_app_dir())
                    sv_sql.set(rel if not rel.startswith('..') else fp)
                except: sv_sql.set(fp)
        ttk.Button(r_sql,text="…",width=3,command=_browse).pack(side="left")

        upd_lf=ttk.LabelFrame(dlg,text="  Aggiornamenti automatici  ",padding=10)
        upd_lf.pack(fill="x",padx=14,pady=4)
        sv_au=tk.BooleanVar(value=self._cfg.getboolean('UPDATE','auto_update',fallback=False))
        sv_interval=tk.StringVar(value=self._cfg.get('UPDATE','check_interval_min',fallback='5'))
        tk.Checkbutton(upd_lf,text="Abilita aggiornamenti automatici",variable=sv_au,
            bg=DARK_BG,fg=TEXT_CLR,selectcolor="#1f6feb",activebackground=DARK_BG,
            font=("Consolas",10),anchor="w").pack(fill="x",pady=(0,4))
        r_int=ttk.Frame(upd_lf); r_int.pack(fill="x")
        ttk.Label(r_int,text="Controlla ogni:",style="Muted.TLabel",width=13).pack(side="left")
        ttk.Entry(r_int,textvariable=sv_interval,width=6,
                  font=("Consolas",10)).pack(side="left",padx=4)
        ttk.Label(r_int,text="minuti",style="Muted.TLabel").pack(side="left")

        info_lf=ttk.LabelFrame(dlg,text="  File configurazione  ",padding=6)
        info_lf.pack(fill="x",padx=14,pady=6)
        tk.Label(info_lf,text=PROGRAMDATA_INI,bg=DARK_BG,fg=ACCENT,
                 font=("Consolas",8),anchor="w").pack(fill="x")

        def _save():
            self._cfg['PLC']={'ip':sv_ip.get().strip(),'rack':sv_rack.get(),
                              'slot':sv_slot.get(),'db':sv_db.get()}
            self._cfg['SQL']={'path':sv_sql.get()}
            self._cfg['UPDATE']={
                'auto_update':        str(sv_au.get()).lower(),
                'check_interval_min': sv_interval.get().strip() or '5',
            }
            if save_settings(self._cfg):
                self._upd_ae_sql()
                if self._update_check_timer:
                    try: self.after_cancel(self._update_check_timer)
                    except Exception: pass
                    self._update_check_timer = None
                if sv_au.get():
                    self.after(200, self._schedule_update_check)
                dlg.destroy()
            else:
                messagebox.showerror("Errore",
                    f"Impossibile scrivere:\n{PROGRAMDATA_INI}",parent=dlg)

        btn_f=ttk.Frame(dlg); btn_f.pack(fill="x",padx=14,pady=8)
        ttk.Button(btn_f,text="💾 Salva",style="Accent.TButton",
                   command=_save).pack(side="left",padx=4)
        ttk.Button(btn_f,text="Annulla",
                   command=dlg.destroy).pack(side="left",padx=4)

    # ── HELPERS ──────────────────────────────────────────────
    def app_log(self, msg, *_): self._lbl_st.config(text=msg)

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

        # (stato pinza rimosso v1.4.1)


# ══════════════════════════════════════════════════════════════════
if __name__=="__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    # Modalità staging (v1.4.10+): «<staging>.upd --apply-update <target_exe> <old_pid>»
    # Il nuovo exe si copia sopra il vecchio (stesso nome/posizione) e lo riavvia.
    if len(sys.argv) >= 3 and sys.argv[1] == "--apply-update":
        _tgt = sys.argv[2]
        try:    _pid = int(sys.argv[3]) if len(sys.argv) >= 4 else 0
        except Exception: _pid = 0
        _apply_update_and_relaunch(_tgt, _pid)   # termina con os._exit

    # Retro-compatibilità con il vecchio schema --replace=<vecchio_exe> (≤ v1.4.9):
    # il vecchio è già chiuso, aspetta 2s in background poi lo cancella.
    _old_to_delete = None
    for _arg in sys.argv[1:]:
        if _arg.startswith("--replace="):
            _old_to_delete = _arg[len("--replace="):]
            break
    if _old_to_delete:
        import threading, time as _time
        def _delete_old(p=_old_to_delete):
            _time.sleep(2)
            try: os.remove(p)
            except Exception: pass
        threading.Thread(target=_delete_old, daemon=True).start()

    app = ThicknessApp()
    app.mainloop()


