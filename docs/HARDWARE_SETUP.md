# 🛠️ Setup su hardware reale — Bazzite + BC-250 (BIOS mod)

Guida passo-passo per portare BUO sulla BC-250 reale con **Bazzite stock**
e **BIOS moddato** (unlock 8 core persistente già attivo).

> Situazione di riferimento: scheda accesa e funzionante, BIOS mod già
> flashato (core CPU sbloccati via DXE), Bazzite in download/installazione.

---

## ✅ 1. Verifiche di base su Bazzite (dopo il primo boot)

```bash
# Versione kernel e Mesa (Bazzite aggiornato li ha già ok)
uname -r                          # deve essere ≥ 6.11
glxinfo -B | grep "OpenGL version"   # deve essere ≥ 25.1

# Sblocco core persistente (BIOS mod): deve risultare già 8
nproc                             # atteso: 8 (16 thread)

# CU GPU: 24 stock (40 dopo la patch)
cat /sys/class/drm/card0/device/num_cu   # atteso: 24 (o 40 se già patchato)

# Governor: NON deve girare prima di BUO
systemctl status cyan-skillfish-governor-smu
```

---

## 🔧 2. Gli script della community

**Nessun passo manuale necessario**: al primo `sudo buo unleash` su
hardware reale, BUO verifica quali tool mancano e li **scarica e
installa da solo** (clona bc250_smu_oc, bc250-40cu-unlock,
bc250-acpi-fix). Se preferisci farlo prima, oppure solo verificare:

```bash
# 👉 Verifica cosa c'è e cosa manca (senza scaricare nulla)
buo install-deps --check

# 👉 Scarica e installa subito (facoltativo: BUO lo fa da solo al primo avvio)
sudo buo install-deps
```

> Il governor è un servizio distro-specifico: BUO lo clona e ti mostra
> le istruzioni (Bazzite: script di `evdokim/bazzite-bc-250-governor`;
> Arch: AUR `cyan-skillfish-governor-smu`). BUO **non** esegue installer
> di terze parti senza la tua conferma.

### In alternativa, a mano:

```bash
# Unlock core CPU (utile come fallback; col BIOS mod è già fatto)
sudo mkdir -p /usr/local/bin

# 40-CU unlock + health test + mask (duggasco)
git clone https://github.com/duggasco/bc250-40cu-unlock
sudo cp bc250-40cu-unlock/scripts/bc250-enable-40cu.sh /usr/local/bin/
sudo cp bc250-40cu-unlock/scripts/bc250-cu-health-test.sh /usr/local/bin/
sudo cp bc250-40cu-unlock/scripts/bc250-cu-mask.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/bc250-*.sh

# Overclock/undervolt CPU (bc250_smu_oc)
git clone https://github.com/bc250-collective/bc250_smu_oc
sudo cp bc250_smu_oc/bc250_detect.py /usr/local/bin/bc250-detect
sudo cp bc250_smu_oc/bc250_apply.py /usr/local/bin/bc250-apply
sudo chmod +x /usr/local/bin/bc250-detect /usr/local/bin/bc250-apply

# Governor SMU su Bazzite (script della community)
git clone https://github.com/evdokim/bazzite-bc-250-governor
bash bazzite-bc-250-governor/install.sh   # oppure seguine il README
```

> ⚠️ La patch **40-CU** richiede kernel-devel: su Bazzite si compila con
> `rpm-ostree install kernel-devel` + reboot, oppure usando il kernel
> CachyOS di `MastaG/linux-cachyos-bc250`.

---

## 📦 3. Installa BUO (Bazzite è immutabile → venv)

Bazzite è ostree-based: non si installano pacchetti Python in `/usr`.
Metodo consigliato: venv + symlink.

```bash
# Copia il progetto (da USB/cloud/git)
git clone https://github.com/andre98king/buo.git
cd buo

# Venv dedicato
sudo mkdir -p /opt/buo
sudo cp -r buo config requirements.txt setup.py pyproject.toml /opt/buo/
cd /opt/buo
sudo python3 -m venv /opt/buo-venv
sudo /opt/buo-venv/bin/pip install -r requirements.txt
sudo /opt/buo-venv/bin/pip install -e .

# Comando globale
sudo ln -sf /opt/buo-venv/bin/buo /usr/local/bin/buo

# Configurazione
sudo mkdir -p /etc/buo
sudo cp config/buo.yaml /etc/buo/buo.yaml

# Verifica
buo --version
buo status            # hardware reale
```

---

## 🚀 4. Prima esecuzione (a passi, con sicurezza)

```bash
# 1. Solo diagnostica — nessuna modifica
buo safety-test

# 2. Prova completa in simulazione
sudo buo unleash --dry-run

# 3. Esecuzione reale, con conferma ad ogni fase (consigliato al primo giro)
sudo buo unleash --interactive

# 4. A regime: tutto automatico
sudo buo unleash
```

Cosa farà BUO in sequenza (vedrai ogni fase nel terminale):

| Fase | Cosa fa | Sul tuo sistema (BIOS mod) |
|:---|:---|:---|
| **pre_audit** | Rileva hardware e problemi noti | Core già 8 → rilevato ✅ |
| **unlock** | CPU 8-core, GPU 40-CU, health test | CPU **saltata** (già sbloccata), GPU patchata se serve |
| **fix** | TLB, ACE, IOMMU, ACPI, GTT, ventole | IOMMU=off, ACPI (metodo ostree/cpio), GTT, nct6683 |
| **optimize** | Undervolt CPU/GPU + overclock power-limited | Binary search sui tuoi chip |
| **apply** | Configura governor con i safe-points | config.toml scritto e servizio riavviato |
| **validate** | Stress test + verifica fix + benchmark after | Report before/after |

---

## 📝 5. Note specifiche per Bazzite

- **ACPI fix**: ⚠️ Su Bazzite/ostree è **MANUALE**: BUO NON scrive più
  `SSDT_ACPI.cpio` su `/boot` (verificato sul campo: può rompere il
  boot). Metodo corretto della community: consulta il repo
  `bc250-acpi-fix` o `bazzite-bc-250-toolkit` (solo C-State: i
  P-State "doesn't work" su questa scheda).

- **Black screen desktop**: se il desktop resta nero, aggiungi nel boot:
  ```
  KWIN_DRM_DEVICES=/dev/dri/card1 KWIN_DRM_NO_AMS=1
  ```

- **IOMMU**: ⚠️ **MAI usare `iommu=off` come parametro kernel**: su BC-250
  rompe la interrupt remapping e causa USB + rete morte (partial hang,
  verificato sul campo — bug #2). La community consiglia di
  disabilitare l'IOMMU **nel BIOS** (Advanced → AMD CBS → NBIO → IOMMU →
  Disabled) per curare eventuali crash/black-screen della **GPU** — è un
  toggle firmware manuale, non un comando da OS. Se trovi `iommu=off` in
  `/proc/cmdline`, rimuovilo con:
  ```bash
  sudo rpm-ostree kargs --delete=iommu=off && reboot
  ```

- **Fix kernel (TLB/ACE)**: richiedono la compilazione dei sorgenti
  kernel/Mesa. BUO li rileva e ti dà le istruzioni; l'esecuzione è
  delegata a `bc250-gfx1013-fix` (ACE) e alla patch TLB della community.

---

## 🧪 6. Se qualcosa va storto

```bash
# Rollback di TUTTO
sudo buo rollback

# Ripresa dopo un reboot durante l'esecuzione
sudo buo recover

# Log completo
cat /var/log/buo/buo.log
```

---

*Guida generata da BUO v1.0.0 — adattata a Bazzite stock + BIOS mod.*
