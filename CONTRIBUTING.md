# 🤝 Contribuire a BC-250 Ultimate Orchestrator

Grazie per l'interesse nel contribuire a BUO! Questo documento fornisce le linee guida per contribuire al progetto.

---

## 📋 Codice di Condotta

Adottiamo il [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/). Rispetta gli altri contributori e la community.

---

## 🚀 Come Iniziare

### 1. Fork e Clone

```bash
git clone https://github.com/andre98king/buo.git
cd buo
```

### 2. Setup Ambiente di Sviluppo

```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

### 3. Branching Strategy

```bash
git checkout -b feature/your-feature-name
git checkout -b fix/your-bug-fix
git checkout -b docs/your-documentation
```

---

## 📝 Linee Guida di Codice

### Python Style (PEP 8) e Type Hints

```python
from typing import List, Dict, Optional

def estimate_vram(gpu_temp: float, gpu_power: float,
                  ambient_temp: float = 22.0) -> float:
    """Stima la temperatura della VRAM posteriore."""
    return ambient + alpha * (gpu_temp - ambient) + beta * gpu_power
```

Ogni funzione/classe deve avere una docstring con Args/Returns/Raises.

### Sicurezza (regola d'oro del progetto)

1. **MAI** modificare gli hard limits in `buo/constants.py` — sono immutabili di proposito
2. Ogni nuova modifica al sistema **deve** avere un rollback registrato in `state/rollback.py`
3. Ogni operazione hardware va testata prima in modalità `--mock` e `--dry-run`
4. I valori di configurazione non possono MAI superare gli hard limits

---

## 🧪 Testing

```bash
# Tutti i test
python -m unittest discover tests

# (se pytest è installato)
pytest --cov=buo tests/
```

Nuovi moduli → nuovi test. I test devono funzionare senza hardware reale (mock).

---

## 🔄 Processo di Pull Request

1. **Fork** il repository
2. **Crea** un branch per la tua feature
3. **Scrivi** codice e test
4. **Assicura** che tutti i test passino
5. **Apri** una Pull Request

### Checklist PR

- [ ] Test passano (`python -m unittest discover tests`)
- [ ] Docstring aggiornate
- [ ] README aggiornato
- [ ] Changelog aggiornato
- [ ] Nessuna regressione

---

## 📝 Commit Messages

Formato: `type(scope): subject`

```bash
feat(cpu): add core stability test
fix(gpu): correct WGP mask generation
docs(readme): update installation guide
test(vram): add unit tests for estimator
refactor(safety): simplify monitor loop
```

**Tipi:** `feat` | `fix` | `docs` | `test` | `refactor` | `chore`

---

## 🔍 Aree di Contributo

| Area | Priorità | Note |
|:---|:---|:---|
| **Testing su hardware reale** | 🔴 Alta | Validare i flussi su BC-250 |
| **Fix kernel (TLB, ACE)** | 🔴 Alta | Automazione con sorgenti kernel |
| **Raccolta dati VRAM** | 🟡 Media | Per il modello ML |
| **Wrappers script esterni** | 🟡 Media | Migliorare parsing/fallback |
| **Documentazione** | 🟢 Bassa | Guide, esempi, traduzioni |
| **Benchmark** | 🟢 Bassa | Più tool e metriche |

---

## 🌐 Community

- **GitHub Issues**: https://github.com/andre98king/buo/issues
- **Discord BC-250**: canale ufficiale della community
- **Documentazione**: https://github.com/elektricM/amd-bc250-docs

---

## 📄 Licenza

Contribuendo, accetti che il tuo codice sia rilasciato sotto la licenza GPLv3 (codice) e CC BY-SA 4.0 (documentazione).

---

**Grazie per contribuire a BUO! 🚀**
