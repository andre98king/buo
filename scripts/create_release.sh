#!/bin/bash
# create_release.sh — genera l'archivio di rilascio del progetto BUO.
# Uso: ./scripts/create_release.sh [versione]

set -euo pipefail

# Cwd-indipendente: i path relativi (--exclude-from=.gitignore, sorgenti)
# valgono rispetto alla radice del repo, da qualunque directory si invochi.
cd "$(dirname "$0")/.."

VERSION="${1:-1.0.0}"
DATE="$(date +%Y%m%d)"
OUT="buo-release-${VERSION}-${DATE}"

echo "📦 Creazione pacchetto BUO v${VERSION}..."

rm -rf "/tmp/${OUT}"
mkdir -p "/tmp/${OUT}"

# Copia il progetto rispettando .gitignore (--exclude-from): i file interni
# (research/, reference/, PROJECT_STATUS.md, docs/BUGS.md, ...) restano fuori
# dall'archivio. Gli exclude espliciti sotto sono una rete di sicurezza.
rsync -a \
  --exclude-from=.gitignore \
  --exclude='*.pyc' --exclude='__pycache__' --exclude='.git' \
  --exclude='venv' --exclude='.venv' --exclude='*.egg-info' \
  --exclude='research/' --exclude='reference/' \
  --exclude='PROJECT_STATUS.md' \
  --exclude='docs/BUGS.md' --exclude='docs/COMMUNITY_NOTES.md' \
  --exclude='docs/8CORE_PLAN.md' --exclude='docs/TEST_PLAN.md' \
  ./ "/tmp/${OUT}/"

cd /tmp

# Archivio tar.gz
tar -czf "${OUT}.tar.gz" "${OUT}"

# Archivio zip
if command -v zip >/dev/null; then
  zip -rq "${OUT}.zip" "${OUT}"
fi

echo ""
echo "✅ Pacchetti creati in /tmp:"
ls -lh "${OUT}.tar.gz" "${OUT}.zip" 2>/dev/null || ls -lh "${OUT}.tar.gz"
echo ""
echo "📦 Installazione:"
echo "   tar -xzf ${OUT}.tar.gz && cd ${OUT}"
echo "   pip install -e ."
echo "   buo status --mock"
