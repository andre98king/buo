# 🚨 RECUPERO BC-250 — Boot failure dopo il fix ACPI

**Cosa è successo (notte del 27/08):** durante il primo test reale, BUO ha
scritto `SSDT_ACPI.cpio` su `/boot` (metodo ACPI pensato per distro non-ostree).
Su **Bazzite/ostree questo file può rompere il boot**: la scheda è rimasta
irraggiungibile (nessun ping, nessun SSH).

**La scheda NON è brickata a livello hardware** — è un problema di boot
software, risolvibile. Il BIOS mod resta intatto.

---

## 👉 Recupero (dalla console della scheda, tastiera+monitor)

### Passo 1 — Avvia il deployment PRECEDENTE

Al menu di boot di Bazzite (GRUB/shim), scegli la voce **"previous"** /
**"rollback"** (il deployment precedente all'ultimo aggiornamento).
Se non c'è menu, spegni e riaccendi tenendo premuto un tasto selettore
(Shift per GRUB).

> Se il sistema parte normalmente, vai al Passo 2.
> Se NON parte nemmeno il deployment precedente, usa il **programmatore
> SPI (CH341A)** o il recupero BIOS della scheda — ma è improbabile:
> il BIOS non è stato toccato in questa sessione.

### Passo 2 — Rimuovi il file che rompe il boot

Appena hai una shell (anche di recupero):

```bash
sudo rm -f /boot/SSDT_ACPI.cpio
sudo systemctl disable --now buo-resume 2>/dev/null
sudo reboot
```

### Passo 3 — Verifica

```bash
ping -c 2 IP_DI_RETE    # dal PC
ssh utente@IP_DI_RETE   # se preferisci da remoto
```

---

## 🔒 Dopo il recupero (quando la scheda è su)

1. Aggiorna BUO alla versione corretta (l'ACPI su ostree ora è manuale):
   ```bash
   cd ~/buo-src && git pull && sudo cp -r buo /opt/buo/ && sudo /opt/buo-venv/bin/pip install -e /opt/buo
   ```
2. Rilancia il test SENZA i passi rischiosi:
   ```bash
   sudo buo unleash --interactive
   ```
   Ora: CPU unlock (già 8 core) → GPU (avviso se manca kernel-devel) →
   fix sicuri (GTT, ventole; IOMMU/ACPI/TLB/ACE manuali con istruzioni) →
   **undervolt reale** (funziona!) → apply → stress → report.

## 📝 Cosa resta manuale su Bazzite (per scelta di sicurezza)

| Passo | Comando manuale |
|:---|:---|
| IOMMU (crash GPU) | disabilita **nel BIOS** (Advanced → AMD CBS → NBIO → IOMMU → Disabled); **NON** `iommu=off` kernel |
| ACPI C-State | metodo della community (`bazzite-bc-250-toolkit`) |
| 40-CU GPU | `sudo rpm-ostree install kernel-devel && reboot`, poi `sudo buo unleash` |
| Governor | COPR/script community (`cyan-skillfish-governor-smu`) |
| TLB / ACE | compilazione kernel/Mesa (`bc250-gfx1013-fix`) |

---

## 🔧 Cosa è stato corretto nel codice (deploy su GitHub)

- **CRITICO**: `buo/fix/acpi.py` — su ostree NON scrive più su `/boot`
  (metodo manuale con istruzioni) — commit `79c743f`
- `buo/fix/iommu.py` — su ostree il fix è manuale (il demone rpm-ostree
  può bloccarsi e appendere il pipeline) — commit `dcfbecc`
- Anti-loop reboot, parser bc250-detect reale, libreria bc250_smu
  installata, cwd scrivibile per bc250-detect, rilevamento Mesa corretto
  (129 test verdi su questo PC)

**Grazie per la pazienza — il test reale ha trovato e fatto correggere
bug che nessun mock avrebbe mai scovato.** 🛠️
