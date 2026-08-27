#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Dashboard HTML autonoma del report BUO.

Genera un singolo file .html (nessuna dipendenza: né plotly né rete)
con i dati del report JSON incorporati e grafici a barre before/after
in JavaScript puro.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.logging import get_logger
from ..utils.paths import state_dir

logger = get_logger("report.dashboard")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>BUO — Report Dashboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto;
         max-width: 900px; color: #1a1a2e; background: #f5f6fa; }}
  h1 {{ color: #0d6efd; }} h2 {{ color: #333; margin-top: 2rem; }}
  .card {{ background: #fff; border-radius: 10px; padding: 1rem 1.5rem;
          margin: 1rem 0; box-shadow: 0 2px 6px rgba(0,0,0,.08); }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ padding: .5rem; border-bottom: 1px solid #eee; text-align: left; }}
  .ok {{ color: #198754; }} .bad {{ color: #dc3545; }} .warn {{ color: #fd7e14; }}
  .bar {{ background: #0d6efd; height: 22px; border-radius: 4px;
         min-width: 2px; transition: width .5s; }}
  .bar.after {{ background: #198754; }}
  svg text {{ font-size: 12px; }}
</style>
</head>
<body>
  <h1>🚀 BC-250 Ultimate Orchestrator — Dashboard</h1>
  <p>Generato: {generated}</p>

  <div class="card"><h2>📋 Riepilogo</h2>
    <table id="summary"></table>
  </div>

  <div class="card"><h2>📊 Benchmark Before/After</h2>
    <div id="charts"></div>
  </div>

  <div class="card"><h2>✅ Verifica Fix</h2>
    <table id="fixes"></table>
  </div>

  <div class="card"><h2>🔍 Problemi Rilevati</h2>
    <ul id="problems"></ul>
  </div>

<script>
const REPORT = {report_json};

// Riepilogo
const s = REPORT.performance_gain || {{}};
const rows = Object.entries(s).map(([k, v]) =>
  `<tr><td>${{k}}</td><td>${{v}}</td></tr>`).join('');
document.getElementById('summary').innerHTML =
  '<tr><th>Metrica</th><th>Guadagno</th></tr>' + rows;

// Benchmark: barre before/after per ogni metrica numerica
const charts = document.getElementById('charts');
const bench = REPORT.benchmarks || {{}};
const before = bench.before || {{}}, after = bench.after || {{}};
for (const [test, bv] of Object.entries(before)) {{
  const av = after[test] || {{}};
  const keys = new Set([...Object.keys(bv), ...Object.keys(av)]);
  const metricKeys = [...keys].filter(k =>
    typeof bv[k] === 'number' || typeof av[k] === 'number');
  if (!metricKeys.length) continue;
  let html = `<h3>${{test}}</h3><table><tr><th>Metrica</th><th>Prima</th><th>Dopo</th><th></th></tr>`;
  for (const k of metricKeys) {{
    const b = typeof bv[k] === 'number' ? bv[k] : 0;
    const a = typeof av[k] === 'number' ? av[k] : 0;
    const max = Math.max(b, a, 1);
    html += `<tr><td>${{k}}</td><td>${{b}}</td><td>${{a}}</td>
             <td><div class="bar" style="width:${{Math.round(b/max*100)}}%"></div>
             <div class="bar after" style="width:${{Math.round(a/max*100)}}%"></div></td></tr>`;
  }}
  html += '</table>';
  charts.insertAdjacentHTML('beforeend', html);
}}

// Fix
const fixes = REPORT.fixes_verification || {{}};
document.getElementById('fixes').innerHTML =
  '<tr><th>Fix</th><th>Stato</th><th>Dettaglio</th></tr>' +
  Object.entries(fixes).map(([n, f]) =>
    `<tr><td>${{n}}</td><td class="${{f.ok ? 'ok' : (f.ok === false ? 'bad' : 'warn')}}">
     ${{f.ok ? '✅' : (f.ok === false ? '❌' : '⚠️')}}</td><td>${{f.detail || ''}}</td></tr>`
  ).join('');

// Problemi
document.getElementById('problems').innerHTML =
  (REPORT.problems_found || []).map(p =>
    `<li class="${{p.severity === 'alta' ? 'bad' : 'warn'}}">
     [<b>${{(p.severity || '?').toUpperCase()}}</b>] ${{p.title || p.id}}</li>`
  ).join('') || '<li>Nessun problema noto</li>';
</script>
</body>
</html>
"""


def generate_html_dashboard(report_json: Optional[Path] = None) -> Path:
    """
    Genera la dashboard HTML dal report JSON.

    Args:
        report_json: percorso del report JSON (default: state_dir/report.json)

    Returns:
        percorso del file .html generato
    """
    path = Path(report_json) if report_json else state_dir() / "report.json"

    if not path.exists():
        raise FileNotFoundError(
            f"Report JSON non trovato: {path} — esegui prima: sudo buo unleash")

    data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    generated = data.get("generated_at", "")

    html = HTML_TEMPLATE.format(
        generated=generated,
        report_json=json.dumps(data, ensure_ascii=False),
    )

    out = path.with_suffix(".html")
    out.write_text(html, encoding="utf-8")
    return out
