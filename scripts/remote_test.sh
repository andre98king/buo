#!/bin/bash
# remote_test.sh — Test remoto one-shot di BUO sulla BC-250 (Bazzite)
#
# Esegui QUESTO sulla BC-250 (console o terminale):
#   bash <(curl -sL https://raw.githubusercontent.com/andre98king/buo/main/scripts/remote_test.sh)
#
# Fa tutto da solo:
#   1. installa BUO (venv, per Bazzite immutabile)
#   2. esegue: buo --version, buo doctor, buo install-deps --check
#   3. dry-run completo (simulazione, nessuna modifica)
#   4. salva TUTTO in /tmp/buo_remote_test.log
# Poi incollaci il contenuto del log (o l'output a schermo).

set -uo pipefail

LOG=/tmp/buo_remote_test.log
exec > >(tee "$LOG") 2>&1

echo "======================================================"
echo " BUO REMOTE TEST — $(date)"
echo "======================================================"
hostname
whoami
uname -r
nproc
echo "------------------------------------------------------"

# 1. Ottieni il progetto (clone shallow dal repo)
cd /tmp || exit 1
if [ ! -d buo ]; then
  echo ">>> git clone buo..."
  git clone --depth 1 https://github.com/andre98king/buo.git || { echo "CLONE FAILITO (rete?)"; exit 1; }
fi
cd buo || exit 1
git pull -q 2>/dev/null || true

# 2. Installa in venv (Bazzite/immutabile)
if [ ! -x /opt/buo-venv/bin/buo ]; then
  echo ">>> setup venv /opt/buo-venv..."
  sudo mkdir -p /opt/buo
  sudo cp -r buo config docs requirements.txt setup.py pyproject.toml /opt/buo/ 2>/dev/null
  sudo python3 -m venv /opt/buo-venv || { echo "VENV FALLITO (python3? pip?)"; exit 1; }
  sudo /opt/buo-venv/bin/pip install -q -r requirements.txt || true
  sudo /opt/buo-venv/bin/pip install -q -e /opt/buo/ || { echo "PIP INSTALL FALLITO"; exit 1; }
  sudo ln -sf /opt/buo-venv/bin/buo /usr/local/bin/buo
fi

# 3. Diagnostica
echo ">>> buo --version"
buo --version
echo ">>> buo doctor"
buo doctor
echo ">>> buo install-deps --check"
buo install-deps --check || true

# 4. Dry-run completo (nessuna modifica)
echo ">>> sudo buo unleash --dry-run --skip-benchmark"
sudo buo unleash --dry-run --skip-benchmark 2>&1 | tail -60 || true

echo "======================================================"
echo " FINE — log completo: $LOG"
echo " Incollami il contenuto del log per la diagnosi."
echo "======================================================"
