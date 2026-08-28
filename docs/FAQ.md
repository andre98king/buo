# ❓ FAQ — BUO

## Generale

**D: BUO è sicuro? Non brickerà la mia BC-250?**
R: BUO non supera mai gli hard limits (VID ≤ 1325 mV, GPU ≤ 1100 mV),
codificati nel codice e non modificabili. Ogni fase salva un checkpoint
e c'è un rollback a cascata per ogni modifica. Prima di qualsiasi
intervento fa una verifica di sanità (kernel, Mesa, temperature) e se un
test di stabilità non è possibile si ferma.

**D: Serve sapere cosa sono SMU, ACPI, WGP?**
R: No. BUO nasconde tutta la complessità: un solo comando, tutto automatico.

**D: Devo installare gli script della community a mano?**
R: No: al primo avvio BUO scarica da solo i tool mancanti
(bc250_smu_oc, bc250-40cu-unlock, bc250-acpi-fix).

## Utilizzo

**D: Cosa succede se si spegne la corrente a metà esecuzione?**
R: Al reboot esegui `sudo buo recover`: riparte dall'ultimo checkpoint.

**D: Come annullo tutto?**
R: `sudo buo rollback` ripristina ogni modifica (rollback a cascata).

**D: Posso provare senza rischiare nulla?**
R: Sì: `sudo buo unleash --dry-run` simula tutto senza modificare nulla.
Oppure `buo probe` per la sola analisi.

**D: Come vedo cosa ho guadagnato?**
R: `buo report` (Markdown) o `buo report --dashboard` (HTML con grafici
before/after).

## Hardware

**D: Ho il BIOS mod con core già sbloccati. Ci sono problemi?**
R: No: BUO rileva i core già attivi (8) e salta l'unlock, senza reboot
inutili.

**D: La VRAM non ha sensori: come la controllate?**
R: BUO stima la temperatura VRAM con un modello empirico (α/β dalla
community) più un modello ML addestrabile con i tuoi dati
(`buo data-collect` con una termocoppia USB, poi `buo ml-train`).

**D: Funziona su Bazzite/SteamOS?**
R: Sì: BUO rileva la distro (ostree-based) e usa il metodo ACPI corretto
(initrd override). Vedi docs/HARDWARE_SETUP.md.

## Risoluzione problemi

**D: Il gioco (Steam Gaming Mode) è "fisso" a 60 FPS anche con 40 CU attive.**
R: Quasi sicuramente è il **refresh dell'output**: gamescope emette alla
modalità preferita dell'EDID — se il monitor dichiara preferito
1920x1080@60Hz (col 144Hz come alternativo), l'output resta 60Hz e i
giochi presentano a 60. Verifica:
`sudo cat /sys/kernel/debug/dri/1/state | grep mode:`.
Fix: Steam → Impostazioni → Display → "Automatically Set Resolution" →
OFF → seleziona 1920x1080@144 (la scelta si salva e persiste).

**D: `buo unleash` si ferma con "bc250-detect non trovato"**
R: È il fail-closed che funziona: manca il tool di undervolt. Esegui
`sudo buo install-deps` (o controlla la rete: BUO lo scarica da solo).

**D: La TUI non parte**
R: Installa `pip install textual` (dipendenza opzionale).

**D: Dove trovo i log?**
R: `/var/log/buo/buo.log` (o `~/.local/state/buo/log/buo.log` senza root).
