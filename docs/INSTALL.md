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
