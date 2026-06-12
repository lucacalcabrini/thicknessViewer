# Changelog

Tutte le modifiche rilevanti a **Thickness Profiler** sono documentate in questo file.

La versione dell'app è definita da `APP_VERSION` in `thickness_viewer_v1_4_6.pyw`.
Pubblicando un tag `vX.Y.Z` la CI builda l'exe e crea la release su GitHub
(da cui l'auto-update dell'app scarica la nuova versione).

## [1.4.22] - 2026-06-12

### Corretto
- **Offset DB aggiornati per FB936 v1.2.** Il nuovo campo `TaraturaDiscoRiferimento : Bool`
  nel UDT sposta `AbilitaTaratura` da bit 34.0 a bit 34.1. La dimensione del UDT rimane
  36 byte (tutti gli altri offset invariati). Aggiunto `TaraturaDiscoRiferimento` nel
  pannello parametri PLC.

## [1.4.21] - 2026-06-11

### Corretto
- **Soglia NOK bidirezionale nel viewer.** Il rilevamento celle fuori soglia ora usa
  `|delta| > SpessoreMassimo` in entrambi i grafici (Profilo e Delta), allineato al
  PLC FB936 v1.2 che controlla sia eccesso (disco troppo spesso) sia difetto (disco
  troppo sottile). Aggiunta zona errore anche sotto la soglia negativa nei grafici,
  linea tratteggiata simmetrica `sp_att ± sg` e fill separato "Difetto oltre soglia".

## [1.4.20] - 2026-06-10

### Corretto
- **Controllo aggiornamenti all'avvio sempre attivo.** Indipendentemente dal flag
  "aggiornamenti automatici", l'app controlla una volta all'avvio se esiste una
  versione più recente su GitHub. Il controllo periodico (timer) rimane opzionale
  e si attiva solo se `auto_update = true`.

## [1.4.19] - 2026-06-10

### Modificato
- **Solo bump di versione** (1.4.18 → 1.4.19) — secondo test del meccanismo di
  auto-update, eseguito interamente in autonomia (commit → push su main → tag →
  build → release). Nessuna modifica funzionale al codice.

## [1.4.18] - 2026-06-10

### Modificato
- **Solo bump di versione** (1.4.17 → 1.4.18) per verificare end-to-end il
  meccanismo di auto-update periodico introdotto nella 1.4.17. Nessuna modifica
  funzionale al codice.

## [1.4.17] - 2026-06-10

### Aggiunto
- **Auto-update periodico e silenzioso.** Nuovo campo nelle Impostazioni
  "Aggiornamenti automatici": se abilitato, l'app controlla GitHub ogni N minuti
  (default 5) e, se trova una versione più recente, scarica e applica l'update
  senza dialog di conferma. Il titolo della finestra mostra il progresso del download.
- **Resume state dopo aggiornamento.** Prima di riavviarsi per applicare un update,
  l'app salva in `C:\ProgramData\ThicknessViewer\resume_state.json` lo stato
  corrente (IP PLC, rack, slot, polling, slot DB auto-export, stato auto-export).
  Alla riapertura i parametri vengono ripristinati automaticamente; se l'auto-export
  era in esecuzione, riparte da solo dopo 500 ms.
- Nuova costante `PROGRAMDATA_RESUME` e funzioni `load_resume_state`,
  `save_resume_state`, `delete_resume_state`.
- `_update_check_timer` cancellato correttamente alla chiusura e al salvataggio
  delle impostazioni.

## [1.4.16] - 2026-06-08

### Corretto
- **Grafico ancora bloccato / statistiche non aggiornate completamente.**
  `ax.clear()` lasciava residui interni di matplotlib (tick locator, limiti,
  artisti del twin-axis). Sostituito con `fig.clf()` + `add_subplot()` in entrambi
  i grafici (Profilo e Delta): la figura viene distrutta e ricostruita da zero
  a ogni aggiornamento, garantendo nessun dato residuo del ciclo precedente.
  Aggiunto `get_tk_widget().update_idletasks()` dopo `draw()` per forzare il
  blit del PhotoImage sullo schermo indipendentemente dallo stato idle di Tk.

## [1.4.15] - 2026-06-08

### Corretto
- **Grafico ancora bloccato con Auto-Export.** Anche `draw()` su TkAgg schedula
  internamente il blit della PhotoImage come evento *idle*, che veniva comunque
  soffocato dal ciclo di polling. Fix: aggiunto `self.update_idletasks()` al
  termine di ogni ciclo `_ae_poll`, che scarica tutti i repaint pendenti **prima**
  di rimettere in coda il prossimo poll.
- `tight_layout()` separato da `draw()` con proprio try/except: se lancia
  (edge case con assi gemelli) non blocca più il disegno del canvas.

## [1.4.14] - 2026-06-08

### Corretto
- **Grafici bloccati con Auto-Export attivo.** Causa: i read PLC (blocco sincrono
  di ~200-300 ms sul main thread) consumavano l'intero intervallo di polling,
  impedendo a Tk di elaborare i callback `draw_idle()` pendenti → canvas mai
  ridisegnato. Fix: cambiato `draw_idle()` → `draw()` in `_draw_profilo` e
  `_draw_delta` (ridisegno sincrono immediato, non rimandato a idle).
- **Roundtrip inutile `gen_db_text`→`parse_db_text` in `_ae_trigger`** rimosso:
  il dict `dec` già pronto viene passato direttamente a `_load_data`, eliminando
  serializzazione/parsing superflui e possibili perdite di precisione.

## [1.4.13] - 2026-05-26

### Rimosso
- **Pulsanti PNG superflui**: rimossi i pulsanti "💾 PNG grafico" (barra superiore),
  "💾 PNG" e "🔄" dai tab Profilo e Delta. Il salvataggio immagine rimane disponibile
  tramite il pulsante 💾 nella toolbar matplotlib in basso (accanto a Zoom).
  Rimosse anche le funzioni `_save_plot` e `_save_plot_current`.

## [1.4.12] - 2026-05-25

### Modificato
- **Griglia Y default cambiato a 0.10 mm**: il passo verticale della griglia nel
  grafico Profilo parte ora da `0.10 mm` (era `0.25 mm`), per una risoluzione più
  fine già al primo avvio. Il valore rimane modificabile tramite il Combobox "Y [mm]".

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
