# 🔒 Security Policy

## Versioni supportate

| Versione | Supportata |
|:---|:---|
| 1.0.0 | ✅ supportata |

BUO è progettato per **modificare parametri hardware reali** (SMU,
frequenze, voltaggi, moduli kernel). Leggi sempre `docs/RECOVERY.md`
prima di eseguirlo su hardware reale: un uso scorretto può causare
instabilità, crash o boot failure.

## ⚠️ Avvertenze di sicurezza fondamentali

1. **BUO esegue modifiche hardware reali** (undervolt/overclock, modprobe,
   unlock CPU/GPU). Un uso scorretto può causare instabilità, crash o
   boot failure.
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
pubblico** per le vulnerabilità: usa la **segnalazione privata** di GitHub
(tab **Security → Report a vulnerability** / private security advisory sul
repo, disponibile a chiunque una volta che il repo è pubblico). Per bug
non sensibili puoi invece aprire un issue normale.

Fornisci: versione BUO, distro (Bazzite/Arch/Fedora), output di
`buo status`, e i passi per riprodurre. Risponderemo entro 7 giorni.

## Pratiche di sviluppo

- I test girano in CI (`python -m unittest discover tests`) su Python 3.10
  e 3.12.
- Le modifiche che toccano `fix/`, `unlock/` o `state/` richiedono test
  dedicati e revisione.
- Ogni bug di campo va documentato (journal interno) con causa e
  prevenzione.
