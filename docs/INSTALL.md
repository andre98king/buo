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
