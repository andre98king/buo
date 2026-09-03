# Setup su hardware reale — Bazzite + BC-250

Guida di riferimento per portare BUO su una **BC-250 reale** con **Bazzite**
(sistema ostree, immutabile). Presuppone: scheda accesa e funzionante,
Bazzite installato e aggiornato.

Il **BIOS mod** (sblocco dei core persistente) è facoltativo: BUO rileva lo
stato reale della scheda e salta i passi già fatti (core già sbloccati →
nessun reboot inutile).

---

## 1. Verifiche di base

```bash
uname -r                          # kernel ≥ 6.11 (BUO lo verifica da solo)
glxinfo -B | grep "OpenGL version"   # Mesa ≥ 25.1
nproc                             # 6 (12 thread) stock, 8 (16) con core sbloccati
```

**GPU: non dare per scontato il numero di card.** La GPU amdgpu può
comparire come `card0` **o** `card1` (su diverse BC-250 è `card1`, senza
`card0`). Per individuarla:

```bash
ls -l /sys/class/drm/card*/device/driver   # la voce che punta ad amdgpu
```

In pratica non serve farlo a mano: `buo status` fa il rilevamento da solo
e mostra core/CU/frequenze reali.

Governor SMU: al primo run normalmente non è ancora attivo
(`systemctl status cyan-skillfish-governor-smu`); lo installa BUO (sezione 2).

## 2. Tool della community — installazione automatica

**Nessun passo manuale**: al primo `sudo buo unleash` BUO verifica i tool
mancanti e li installa da solo. Se preferisci farlo prima, o solo
verificare:

```bash
buo install-deps --check        # verifica cosa c'è e cosa manca
sudo buo install-deps           # scarica e installa i tool mancanti
```

BUO installa **solo** repo note e pinnate a un commit verificato
(`bc250_smu_oc`, `bc250-40cu-unlock`, `bc250-cu-live-manager`,
`bc250-acpi-fix`, `bc250_memcfg`) e copia gli script in `/usr/local/bin`.

Il **governor GPU** (`cyan-skillfish-governor-smu`) e **umr** non si
clonano: sono **pacchetti del package manager della distro**, installati
automaticamente da BUO (`deps.auto_install_governor: true` di default):

- Bazzite/Fedora: COPR `filippor/bazzite` (abilitato da BUO) + `dnf` /
  `rpm-ostree`;
- Arch: AUR `cyan-skillfish-governor-smu`;
- `umr`: `rpm-ostree install umr` (attivo al prossimo reboot).

BUO non esegue **mai** installer di terze parti e scrive una configurazione
di default sicura per il governor.

## 3. Installa BUO (Bazzite è immutabile → venv)

Bazzite è ostree-based: non si installano pacchetti Python in `/usr`. Metodo
consigliato: venv dedicato + symlink.

```bash
git clone https://github.com/andre98king/buo.git
cd buo

sudo python3 -m venv /opt/buo-venv
sudo /opt/buo-venv/bin/pip install -r requirements.txt
sudo /opt/buo-venv/bin/pip install -e .

sudo ln -sf /opt/buo-venv/bin/buo /usr/local/bin/buo

sudo mkdir -p /etc/buo
sudo cp config/buo.yaml /etc/buo/buo.yaml
```

Nota: l'installazione `-e` resta agganciata alla copia del codice — tienila
in un percorso stabile (es. `/opt/buo` o la tua home). Verifica:

```bash
buo --version
buo status            # hardware reale
```

## 4. Prima esecuzione (a passi)

```bash
buo safety-test                  # 1. solo diagnostica, nessuna modifica
sudo buo unleash --dry-run       # 2. prova completa in simulazione
sudo buo unleash --interactive   # 3. esecuzione reale, conferma per fase
sudo buo unleash                 # 4. a regime: tutto automatico
```

Cosa farà BUO in sequenza (vedrai ogni fase nel terminale):

| Fase | Cosa fa |
|:---|:---|
| **init / pre_audit** | Verifica di sanità, discovery hardware, problemi noti |
| **unlock** | Sblocco core CPU (8) e GPU 40-CU, con health test per ogni passo |
| **fix** | Fix di sistema: ACPI (su ostree: initramfs concatenato), GTT, ventole; TLB/ACE/IOMMU verificati o con istruzioni |
| **optimize** | Ricerca del punto undervolt/overclock stabile per il tuo silicio (stress test reale su ogni candidato) |
| **apply** | Applica e rende persistente la configurazione trovata |
| **validate** | Stress test finale, verifica dei fix, report before/after |

BUO può riavviare la macchina a metà percorso e **riprendere da solo** dal
checkpoint dopo il reboot (`sudo buo recover`, alias `resume`).

## 5. Note specifiche per Bazzite

- **ACPI (C-State)**: su ostree BUO usa il metodo dell'**initramfs
  concatenato**: cpio ACPI + initramfs in un unico blob
  (`initramfs-acpi-<ver>.img`) con una sola riga `initrd` nella boot entry
  (entry sempre backup-ata, idempotente, fail-closed; l'initramfs
  originale non viene toccato). ⚠️ Un cpio separato scritto su `/boot`
  (es. `/boot/SSDT_ACPI.cpio`) ha causato boot failure su ostree: non
  usarlo.
- **Black screen del desktop**: su alcune BC-250 la sessione grafica può
  restare nera dopo l'unlock GPU. Workaround noto: forzare il device giusto
  (adatta `cardN` alla tua macchina, sezione 1):
  `KWIN_DRM_DEVICES=/dev/dri/cardN KWIN_DRM_NO_AMS=1`
- **IOMMU**: ⚠️ **MAI usare `iommu=off` come parametro kernel**: su BC-250
  rompe la interrupt remapping e causa USB + rete morte. Se serve
  disattivare l'IOMMU per la GPU, fallo **nel BIOS** (Advanced → AMD CBS →
  NBIO → IOMMU → Disabled), mai da sistema. Se trovi `iommu=off` in
  `/proc/cmdline`:
  ```bash
  sudo rpm-ostree kargs --delete=iommu=off && reboot
  ```
- **Fix kernel (TLB/ACE)**: richiedono la compilazione di kernel/Mesa
  (es. `bc250-gfx1013-fix`). BUO li rileva e mostra le istruzioni;
  l'esecuzione resta manuale e consapevole.

## 6. Se qualcosa va storto

```bash
sudo buo rollback        # annulla ogni modifica (rollback a cascata)
sudo buo recover         # riprende dal checkpoint dopo un reboot
sudo buo restore         # riporta la macchina allo stato salvato
cat /var/log/buo/buo.log # log completo
buo doctor               # diagnostica da incollare quando chiedi aiuto
```
