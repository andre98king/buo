# 🔧 Installazione — BUO

## Requisiti

- Linux (Fedora 43, Bazzite, CachyOS/Arch consigliati; Debian parziale)
- Python ≥ 3.10
- `git` (per il download automatico dei tool della community)

## 1. Ottieni il codice

```bash
git clone https://github.com/andre98king/buo.git
cd buo
```

## 2. Installa

```bash
# Dipendenze minime (CLI completa)
pip install -r requirements.txt

# (Opzionale) TUI cockpit
pip install textual          # oppure: pip install -e '.[tui]'

# (Opzionale) ML VRAM
pip install numpy scikit-learn

# Installa il comando
sudo pip install -e .

# Directory di sistema e configurazione
sudo mkdir -p /var/lib/buo /var/log/buo /etc/buo
sudo cp config/buo.yaml /etc/buo/
```

## 3. Verifica

```bash
buo --version
buo status --mock        # simulazione senza hardware
buo install-deps --check # controlla i tool della community
```

## 4. Primo avvio reale

```bash
buo safety-test                      # diagnostica
sudo buo unleash --dry-run           # vedi il piano
sudo buo unleash --interactive       # esecuzione guidata
```

Al termine di un run riuscito, BUO salva automaticamente il **profilo
macchina** (punti undervolt/overclock, fix applicati). Il profilo si
esporta/importa e permette il ripristino completo:

```bash
sudo buo profile export --output profilo.json   # backup del profilo
sudo buo profile import profilo.json            # ripristina il profilo
```

## 5. Ripristino dopo un format (o un aggiornamento ostree)

Reinstalla BUO come sopra, poi:

```bash
sudo buo restore                # riporta la macchina allo stato salvato
sudo buo restore --profile profilo.json   # da un profilo esportato
sudo buo restore --validate     # esegue anche lo stress test
```

`restore` riapplica in sicurezza: toolchain, fix ACPI (initramfs
concatenato su ostree), unlock CPU (8 core) e 40 CU, undervolt
persistente e governor — **senza rilanciare l'auto-tuning**.

## 6. Installazione offline (senza rete)

Se la BC-250 non ha accesso a internet, BUO non può clonare i tool della
community. Genera un **bundle offline** su una macchina connessa e
trasferiscilo con una chiavetta USB. Il bundle (`buo-bundle.tar.gz`)
contiene **solo i checkout pinnati e verificati** — manifest
`buo-bundle.json` con commit attesi e tree-hash — mentre i pacchetti
distro (**governor** e **umr**) **non** viaggiano nel bundle: si
installano sempre dal package manager della distro.

### 6.1 Su una macchina CON rete: installa ed esporta il bundle

```bash
sudo buo install-deps                  # clona i checkout pinnati (serve la rete)
sudo buo install-deps --export-bundle buo-bundle.tar.gz
```

L'export è **fail-closed**: se un checkout manca, non è al commit atteso o
non è pulito, BUO non scrive nulla e stampa l'elenco dei problemi:

```
❌ Export bundle fallito: checkout non verificabili: <motivo>
```

A successo l'output è (lo SHA-256 serve per la verifica d'integrità):

```
✅ Bundle offline creato: buo-bundle.tar.gz
SHA-256: <hash> (verifica con: sha256sum buo-bundle.tar.gz)
```

Copia `buo-bundle.tar.gz` su una chiavetta USB.

### 6.2 Sulla BC-250 SENZA rete: importa e installa

```bash
sudo buo install-deps --offline /media/USB/buo-bundle.tar.gz
```

Output atteso (le righe per singolo tool dipendono da cosa manca):

```
📦 Import del bundle offline e installazione...

  ✅ bc250_smu_oc — installata
  ✅ bc250-40cu-unlock — installata
  ✅ bc250-acpi-fix — installata
  …
  ✅ system — stress presente

✅ Dipendenze installate — ora puoi eseguire: sudo buo unleash
```

L'import esegue **9 controlli di integrità fail-closed** (tarball valido,
manifest conforme, commit attesi == catalogo, completezza, estrazione
sicura, tree-hash, verifica git se disponibile, conflitti con deps_dir):
un bundle **obsoleto, parziale o manomesso viene rifiutato**, per esempio:

```
❌ bundle obsoleto: atteso <commit> per bc250_smu_oc, bundle ha <commit>. Rigenera il bundle con: sudo buo install-deps --export-bundle <file> su una macchina con rete
```

Rigenera il bundle con la versione corrente di BUO e riprova. Se invece un
checkout pinnato e verificato è **già presente** in deps_dir (commit atteso
+ albero pulito, verifica A7), BUO lo **riusa senza rete**, senza toccare
il bundle.

### 6.3 Alternativa: lascia che sia `unleash` a importarlo

Su una macchina formattata puoi saltare il passaggio esplicito:
l'orchestratore importa il bundle **prima dell'auto-install**.

```bash
sudo buo unleash --offline-bundle /media/USB/buo-bundle.tar.gz
```

oppure impostalo una sola volta in `/etc/buo/buo.yaml`:

```yaml
deps:
  auto_install: true
  offline_bundle: /media/USB/buo-bundle.tar.gz   # importato prima dell'auto-install
```

Senza rete **né** bundle, `unleash` fallisce con le istruzioni esatte
(export → USB → import → riprova):

```
Download automatico non possibile: <motivo> Controlla la connessione e `git`, oppure usa il bundle offline:
  1) su una macchina CON rete:   sudo buo install-deps --export-bundle bundle.tar.gz
  2) copia il file su USB e importalo qui:
       sudo buo install-deps --offline /percorso/bundle.tar.gz
  3) oppure imposta deps.offline_bundle in /etc/buo/buo.yaml e riprova: sudo buo unleash
```

> **Nota:** `--export-bundle` e `--offline` si escludono a vicenda, e
> `--check` (sola lettura) non si combina con nessuno dei due. Il bundle
> copre i tool clonati da git (script, tabelle ACPI, build); governor e
> `umr` restano pacchetti distro: su una macchina completamente isolata
> servono comunque i repository del package manager (o un mirror locale).

## Bazzite/ostree (immutabile)

Usa un venv (vedi [HARDWARE_SETUP.md](HARDWARE_SETUP.md)):

```bash
sudo python3 -m venv /opt/buo-venv
sudo /opt/buo-venv/bin/pip install -r requirements.txt
sudo /opt/buo-venv/bin/pip install -e .
sudo ln -sf /opt/buo-venv/bin/buo /usr/local/bin/buo
```

## Disinstallare

```bash
sudo buo rollback        # annulla ogni modifica hardware
pip uninstall buo
```
