# Discovery checkpoint - SwissBiotech (2026-07-21)

Breve riepilogo delle operazioni eseguite durante il debug della discovery:

- Aggiunta: fallback Playwright in `discover.py` per gestire 403 del sito
  directory (SwissBiotech) quando `requests` viene bloccato.
- Azione: test locale eseguito — `discover_from_swissbiotech(max_companies=10)`
  ha restituito 10 record (es. `3tbiosciences.com`, `4bases.ch`).
- Nota: molte voci non espongono una `career_url` esplicita; il fallback
  attuale tenta anche la root `https://<domain>` durante la scansione
  (quando applicabile).

Comandi utili per riprodurre il test locale:

```bash
# lancia discovery semplice
python - <<'PY'
import json, discover
res = discover.discover_from_swissbiotech(max_companies=10)
print('found:', len(res))
print(json.dumps(res[:5], indent=2, ensure_ascii=False))
PY

# discovery + scan (timeout 60s per sito)
python - <<'PY'
import json, discover
res = discover.discover_and_scan(source='swissbiotech', max_results=10, per_site_timeout=60)
print(json.dumps(res, indent=2, ensure_ascii=False))
PY

# in caso di risultati inattesi, pulire la cache di discovery e riprovare
rm -f data/discovery_cache.json
```

Prossimi passi suggeriti:

- Integrare l'import manuale delle scoperte nello staging (endpoint di
  approvazione) — utile per evitare falsi positivi.
- Migrare `data/` a SQLite per affidabilità e query avanzate.
- Aggiungere test unitari per `discover_from_swissbiotech` e
  `geocode_location`.

File toccati:

- `discover.py` (aggiunto fallback Playwright)
- `README.md` (formattazione/emoji)
- `docs/discovery-checkpoint.md` (questo file)

Se vuoi che committi e pushi questi cambiamenti su GitHub, dimmi quale
messaggio di commit preferisci e se vuoi che io apra la PR (se lavori su
una feature branch).