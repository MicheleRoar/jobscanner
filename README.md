# jobscanner ⚡️

Stop refreshing careers pages by hand - jobscanner finds new job postings
for you and keeps a tidy dashboard to manage applications, favorites and
follow-ups. It's lightweight, self-hosted and focused on ML/AI/bioinfo
roles around French-speaking Switzerland, but fully configurable.

🚀 Key features

- Daily scans of configured career pages (Playwright/Chromium).
- Multilingual keyword matching (EN/FR/IT) tuned for ML / LLM / CV / bioinfo.
- Tracks new/closed postings, favorites, notes, and spontaneous-application
  statuses with follow-up reminders.
- Discovery helpers: find company career pages automatically (SwissBiotech
  directory integration + Google CSE support).
- Staging for discovered sites — review before adding to your scan list.

⚠️ Opinionated constraints

- Designed for single-user, home/server use. If you expose the port to
  the internet, protect it with a reverse proxy or VPN — there is no
  built-in authentication by default.

Requirements 🧰

- Python dependencies are in `requirements.txt` (Flask, Playwright,
  APScheduler, Requests, BeautifulSoup).

Quick start (local) ▶️

1. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

2. Run locally:

```bash
python app.py
# then open http://localhost:5001
```

Discovery (Swiss Biotech) 🔎

You can run an automatic discovery of biotech companies listed on
SwissBiotech and probe their career pages (no API keys required):

```bash
curl "http://localhost:5001/discover?source=swissbiotech"
```

This will return a JSON list of discovered companies with:
- `name`, `domain`, `profile_url`, `career_url`, `location`, `distance_km`

Security & data 🔐

- State lives in `data/jobs_data.json` and `data/sites.json`. Don't commit
  these files (they may contain personal notes and state).
- `add-site` is validated to reduce SSRF risk (scheme/domain checks,
  private IP blocking, reachability checks).
What's next (ideas)

- Migrate storage to SQLite for safer concurrent writes and richer queries.
- Add CV matching and explainable scoring.
- Generate personalized cover letters from templates.

Contributing

PRs and issues welcome. If you add new discovery sources, try to return
results in staging so users can review before adding to the scan list.

Enjoy - and let me know if you want me to wire the discovery results
directly into the scanner or to a SQLite backend! ✨
