# Changelog

Tutte le modifiche rilevanti a **Thickness Profiler** sono documentate in questo file.

La versione dell'app è definita da `APP_VERSION` in `thickness_viewer_v1_4_6.pyw`.
Pubblicando un tag `vX.Y.Z` la CI builda l'exe e crea la release su GitHub
(da cui l'auto-update dell'app scarica la nuova versione).

## [1.4.8] - 2026-05-25

### Aggiunto
- **Grafico Profilo — Media celle OK**: nuova linea (verde tratteggiata) che mostra
  la media calcolata escludendo le celle fuori soglia (delta > SpessoreMassimo).
  Attivabile/disattivabile con il checkbox "Media celle OK" nella barra del tab.
  Quando attiva, compare anche nel box statistiche come `med.ok`.

### Modificato
- Label della linea spessore medio cambiata in "Spessore medio (tutte)" per distinguerla
  dalla nuova "Media celle OK".

### Dipendenze e CI
- `requirements.txt`: versioni pinnate alle ultime disponibili
  (`matplotlib==3.10.9`, `numpy==2.4.6`, `python-snap7==3.0.0`, `pyinstaller==6.20.0`).
- CI: Python aggiornato `3.12` → `3.13`.
- CI: action aggiornate a Node 24 nativo, rimosso workaround `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24`
  (`checkout` v4→v6, `setup-python` v5→v6, `upload-artifact` v4→v7, `action-gh-release` v2→v3).

## [1.4.7] - 2026-05-25

### Aggiunto
- **Grafico Profilo**: lo spessore medio è ora visibile direttamente sul grafico:
  - linea orizzontale **Spessore medio** con etichetta del valore (es. `2.985 mm`);
  - **Banda ±σ** (deviazione standard) ombreggiata attorno alla media;
  - **Box statistiche** in alto a sinistra: media, min, max, dev.std, celle valide,
    celle vicino soglia e celle NOK.
- **Grafico Delta**: aggiunta linea **Delta medio** (con segno) e relativo **Box statistiche**.
- Interruttori (checkbox) dedicati per accendere/spegnere ogni nuovo elemento dei grafici.

### Modificato
- Le statistiche sono calcolate sulle sole celle valide effettivamente disegnate,
  così la media mostrata corrisponde esattamente alla curva.

### Rimosso
- Pagina **Auto-Export**: rimossi i pulsanti "← Copia DB da PLC Reader" e
  "✗ Deseleziona tutti" (e relative funzioni), considerati non necessari.

## [1.4.6] - 2026-05-20

### Corretto
- Fix polling `label`.

### Rimosso
- Tab Impostazioni.

### Modificato
- Popup più alto.

## [1.4.5]

### Modificato
- Auto-update senza file `.bat`: pattern `--replace=` come in WeldFind.

## [1.4.4]

### Modificato
- Impostazioni come popup in stile WeldFind, percorso fisso in ProgramData.

## [1.4.3]

### Aggiunto
- Icona dell'eseguibile.

### Corretto
- Fix del dialogo carica/salva parametri.

## [1.4.2]

### Aggiunto
- Primo rilascio pubblico: Thickness Profiler con auto-update e build CI.
- Ricerca `.ini`/`.par` multi-path e pulsante "Carica" nel tab Impostazioni.
- Workflow GitHub Actions: build exe e creazione release sui tag `v*`.
