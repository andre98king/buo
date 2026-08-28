# 🔒 Security Policy

## Versioni supportate

| Versione | Supportata |
|:---|:---|
| 1.0.0 (pre-alpha) | ✅ con segnalazioni su GitHub |

BUO è in fase **pre-alpha**: lo usi sulla tua scheda a tuo rischio.
È progettato per **modificare parametri hardware** (SMU, frequenze,
voltaggi, moduli kernel). Leggi sempre `docs/BUGS.md` e `docs/RECOVERY.md`
prima di eseguirlo su hardware reale.

## ⚠️ Avvertenze di sicurezza fondamentali

1. **BUO esegue modifiche hardware reali** (undervolt/overclock, modprobe,
   unlock CPU/GPU). Un uso scorretto può causare instabilità, crash o
   boot failure (vedi `docs/BUGS.md`).
2. **Su Bazzite/ostree**: BUO **non** scrive mai `/boot` e non modifica
   l'initramfs. Ma installa **automaticamente** i pacchetti dal package
   manager della distro: `rpm-ostree install umr` su ostree, `dnf` + COPR
   `filippor/bazzite` per il governor su Fedora/Bazzite, AUR via
   `yay`/`paru` su Arch. Non esegue **mai** installer di terze parti:
   clona solo repo note (pinnate a un commit esatto e verificato) e copia
   gli script; governor e umr arrivano esclusivamente come pacchetti
   ufficiali distro/COPR/AUR.
3. **Hard limit immutabili**: VID CPU ≤ 1325 mV, voltaggio GPU ≤ 1100 mV.
   Questi limiti NON sono sovrascrivibili via config.
4. **Fail-closed**: se un test di stabilità non è possibile, BUO rifiuta di
   procedere invece di inventare valori.

## Segnalare una vulnerabilità

Hai trovato un bug di sicurezza (es. un limite scavalcabile, una scrittura
pericolosa su hardware, una race nel checkpoint)? **Non aprire un issue
pubblico.** Contattaci in privato:

- Apri un **security advisory** privato su GitHub: tab **Security → Report
  a vulnerability** (solo per collaboratori del repo) oppure
- Email al maintainer (vedi `git log` / `pyproject.toml` per il contatto).

Fornisci: versione BUO, distro (Bazzite/Arch/Fedora), output di
`buo status`, e i passi per riprodurre. Risponderemo entro 7 giorni.

## Pratiche di sviluppo

- I test girano in CI (`python -m unittest discover tests`) su Python 3.10
  e 3.12.
- Le modifiche che toccano `fix/`, `unlock/` o `state/` richiedono test
  dedicati e revisione.
- Ogni bug di campo va documentato in `docs/BUGS.md` con causa e
  prevenzione.
