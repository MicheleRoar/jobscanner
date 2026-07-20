# jobscanner

**Stop refreshing 90 careers pages by hand.** jobscanner is a small, self-hosted
job board watcher: point it at the careers pages you care about, and every
morning it tells you what's new — filtered to the roles that actually match
your profile, with a clean dashboard to track applications, favorites, and
follow-ups.

Built for one specific job hunt (ML / LLM / computer vision / bioinformatics
roles around French-speaking Switzerland — EPFL, SIB, CHUV, and ~90 startups
and companies), but the whole thing is just a keyword list and a site
catalog in JSON, so it's easy to repoint at a different field or region.

## What it actually does

- Crawls ~80 careers pages a day (headless Chromium via Playwright),
  follows pagination across multiple pages, and matches postings against a
  multilingual (EN/FR/IT) keyword list.
- Handles a handful of ATS platforms (Workable, SmartRecruiters) through
  their public APIs directly, since their pages render postings as
  JS-only cards that a plain crawler can't see.
- Each site scan runs in its own process with a hard timeout — one slow or
  hung page can't stall the whole run.
- Tracks what's new since the last scan, what's closed, what you've
  applied to, and what you've dismissed as irrelevant.
- Lets you star postings as favorites, add private notes, and mark
  spontaneous-application-only companies with a follow-up reminder after
  21 days of silence.
- Sends a summary email when new postings show up (optional — the app
  works fine without SMTP configured, it just won't email you).
- One dashboard, no login, no database — everything lives in a JSON file
  on disk.

## Honest limitations

- "Relevant posting" detection is keyword matching on link text. It works
  well on most classic careers portals, but on unusual sites it may find
  nothing, or a false positive or two. If a site stops giving sensible
  results, it's worth opening it by hand once in a while to check it
  hasn't changed structure — the dashboard's status table flags sites at
  0 results or in error to make this easy.
- This is not an enterprise tool: it's meant to run on a home NAS or a
  spare machine, for a single user. There's no authentication on the
  dashboard — if you expose the port to the internet, put it behind a VPN
  or a reverse proxy with a login.

## Requirements

- A Synology NAS with **DSM 7** and the **Container Manager** package
  (found in Package Center; on older DSM versions it was called "Docker" —
  the steps are the same). Or really, anything that runs Docker Compose.

## Installation (step by step)

1. **Copy this folder to your NAS.** Open File Station, create a folder
   (e.g. `docker/jobscanner`), and drag in all the files from this project
   (including the `templates/`, `static/`, and `data/` subfolders).

2. **Configure email.** In that folder, rename `.env.example` to `.env`
   and open it with a text editor. Fill in at least `SMTP_HOST`,
   `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`. For Gmail you need an "app
   password" (not your regular account password) — generate one at
   https://myaccount.google.com/apppasswords

3. **Open Container Manager** → **Project** tab → **Create**.
   - Project name: `jobscanner`
   - Path: select the folder you copied in step 1
   - Container Manager will auto-detect `docker-compose.yml`
   - Click **Next** then **Done**: the first build takes a few minutes
     (it downloads the image with Chromium bundled in, about 1–1.5 GB).

4. **Open the dashboard.** Go to `http://<your-nas-ip>:5000` in a
   browser. If you set `RUN_ON_STARTUP=true` in `.env`, the first scan
   kicks off automatically (it can take a while to crawl every site).

5. **Done.** From here on the container just runs, scans every day at the
   time set in `SCAN_TIME` (07:00 by default), and emails you if it finds
   something new. You can also force an immediate scan from the "Scan
   now" button in the dashboard.

## Updating the site list

Open `sites.json` and add/remove entries. Each entry has:
- `id`: short, unique identifier
- `name`: name shown in the dashboard
- `url`: the "careers" page to scan
- `scan`: `true` to scan it, `false` if the site only accepts spontaneous
  applications (it'll be shown in a separate reminder list instead,
  without wasting time scanning it)

After editing, restart the container from Container Manager (Project →
jobscanner → Restart) — no need to rebuild the image.

## Backup

All state (postings already seen, applications, favorites, notes) lives in
`data/jobs_data.json`. If you delete it, the monitor "forgets" what it had
already seen and the next scan will treat everything as new — useful if
you want to start fresh, best avoided if you don't want an email with
dozens of "new" postings you already knew about.

## Stack

Flask + APScheduler for the app and daily schedule, Playwright/Chromium
for crawling, plain JSON files for storage — no database to set up or
maintain.
