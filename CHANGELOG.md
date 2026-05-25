# Changelog

Tutte le modifiche rilevanti a **Thickness Profiler** sono documentate in questo file.

La versione dell'app è definita da `APP_VERSION` in `thickness_viewer_v1_4_6.pyw`.
Pubblicando un tag `vX.Y.Z` la CI builda l'exe e crea la release su GitHub
(da cui l'auto-update dell'app scarica la nuova versione).

## [1.4.11] - 2026-05-25

### Corretto
- **Pulsanti grigi nella barra strumenti del grafico.** I due quadrati grigi tratteggiati
  erano i pulsanti Back/Forward di matplotlib, che nascono *disabilitati* (nessuna
  cronologia zoom) e Tk li disegnava grigi; c'erano inoltre i separatori grigi e il
  pulsante "Subplots" inutile. La toolbar è stata ridotta (`_MiniToolbar`) ai soli
  pulsanti utili e sempre attivi: **Home, Pan, Zoom, Salva**. Niente più elementi grigi.

## [1.4.10] - 2026-05-25

### Corretto (auto-update)
- **Il nuovo exe non si avviava da solo dopo l'aggiornamento.** Causa: alla chiusura
  l'app eseguiva `taskkill /F /T` che, con il flag `/T`, uccideva anche il processo
  figlio appena lanciato (il nuovo exe). Ora durante l'update si usa `taskkill` senza
  `/T` (flag `_RESTARTING`), così il nuovo processo sopravvive e parte regolarmente.
- **Dopo l'update l'exe cambiava nome e posizione sul desktop.** Causa: il nuovo file
  veniva salvato col nome versionato dell'asset (`ThicknessProfiler_vX.Y.Z.exe`), diverso
  dal precedente. Ora l'aggiornamento avviene **in-place**: il nuovo exe viene scaricato
  in un file di staging `<exe>.upd`, si **copia sopra l'exe esistente mantenendo nome e
  posizione**, poi si riavvia. L'icona sul desktop non si sposta più.

### Modificato
- Nuova modalità interna `--apply-update <target> <pid>` per il riavvio in-place;
  mantenuta retro-compatibilità con il vecchio schema `--replace=`.
- Pulizia all'avvio estesa anche ai file temporanei `*.upd`.

> Nota: il fix ha effetto sugli aggiornamenti **a partire da questa versione**. L'update
> *verso* la 1.4.10 fatto da una versione precedente (≤1.4.9) usa ancora il vecchio
> meccanismo difettoso: potrebbe servire avviare manualmente l'exe una volta. Dalla
> 1.4.10 in poi l'aggiornamento è automatico e senza spostamenti.

## [1.4.9] - 2026-05-25

### Aggiunto
- **Griglia configurabile (tab Profilo)**: nuova riga "Griglia:" sotto la barra checkbox
  con due Combobox per scegliere il passo delle linee griglia:
  - **Y [mm]**: 0.10 / **0.25** (default) / 0.50 / 1.00
  - **X [mm]**: 5 / **10** (default) / 20 / 50
  Il grafico si ridisegna automaticamente al cambio selezione.
- **Strumenti grafico visibili**: la barra Pan/Zoom/Home di matplotlib era completamente
  invisibile (nero su nero). Ora ha sfondo distinto (`#1c2128`), pulsanti rialzati
  (`#2d333b`, hover accent blu) e un'etichetta "🛠 Strumenti grafico →" che spiega
  la funzione dei controlli. I pulsanti disponibili sono:
  - 🏠 Home — ripristina la vista iniziale
  - ← / → — naviga avanti/indietro nella cronologia zoom
  - ✋ Pan — trascina il grafico
  - 🔍 Zoom — seleziona un rettangolo per lo zoom
  - 💾 Salva — salva il grafico come immagine

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
