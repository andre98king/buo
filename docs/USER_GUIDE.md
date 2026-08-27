# 📖 Guida Utente — BUO (BC-250 Ultimate Orchestrator)

Guida introduttiva per chi vuole usare BUO sulla propria BC-250 senza
conoscere i dettagli interni (SMU, ACPI, patch kernel...).

---

## 🎯 Cosa fa BUO in una frase

> Un solo comando analizza la tua scheda, sblocca tutto l'hardware,
> risolve i problemi noti, trova il miglior compromesso
> prestazioni/consumi e ti mostra quanto hai guadagnato.

## 🚀 Utilizzo base

```bash
# TUTTO automatico (consigliato al primo giro: conferma ogni fase)
sudo buo unleash --interactive

# A regime
sudo buo unleash

# Solo per vedere cosa succederebbe (nessuna modifica)
sudo buo unleash --dry-run
```

## 🔍 Comandi utili

| Comando | Cosa fa |
|:---|:---|
| `buo status` | Stato hardware (core, CU, temperature) |
| `buo probe` | Solo analisi, nessuna modifica |
| `buo report` | Report finale (before/after) |
| `buo report --dashboard` | Dashboard HTML con grafici |
| `sudo buo rollback` | Ripristina tutto (rollback a cascata) |
| `sudo buo recover` | Riprende dopo un reboot durante l'esecuzione |
| `buo tui` | Cockpit live a schermo intero (richiede textual) |
| `buo safety-test` | Verifica la sicurezza del sistema |

## 🛡️ Sicurezza per l'utente

- BUO **non supera mai** i limiti hardware (VID ≤ 1325 mV, GPU ≤ 1100 mV):
  sono codificati nel codice, non modificabili
- Ogni fase salva un **checkpoint**: se qualcosa va storto, `buo rollback`
  riporta tutto allo stato precedente
- `--dry-run` simula tutto senza toccare nulla
- Se un test di stabilità non può essere eseguito, BUO **si ferma**
  (mai valori non verificati)

## ⚠️ Prima di partire

1. Kernel ≥ 6.11 e Mesa ≥ 25.1 (BUO lo verifica da solo)
2. Connessione internet al primo avvio (BUO scarica i tool della
   community automaticamente)
3. Raffreddamento adeguato (2 ventole 120mm consigliate)
4. Backup del BIOS (utile in ogni caso)

## 🆘 Se qualcosa va storto

```bash
sudo buo rollback     # annulla tutto
sudo buo recover      # riprende da dove si era fermato
cat /var/log/buo/buo.log   # log completo
```

Vedi anche [FAQ.md](FAQ.md) per le domande frequenti.
