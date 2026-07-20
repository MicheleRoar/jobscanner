import os
import threading
from datetime import date, datetime

from flask import Flask, redirect, render_template, request, url_for
from apscheduler.schedulers.background import BackgroundScheduler

import scraper

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")

scan_state = {
    "running": False,
    "message": "",
    "started_at": None,
}


def reset_scan_state():
    scan_state.update({"running": False, "message": "", "started_at": None})


def get_scan_state():
    return {
        "running": scan_state["running"],
        "message": scan_state["message"],
        "started_at": scan_state["started_at"],
    }


def set_scan_state(running, message=""):
    scan_state["running"] = running
    scan_state["message"] = message
    if running:
        scan_state["started_at"] = datetime.now().strftime("%H:%M:%S")
    elif not message:
        scan_state["started_at"] = None


scan_lock = threading.Lock()


def scheduled_job():
    run_scan_in_background()


def run_scan_in_background():
    # two scans running in parallel (e.g. button pressed twice, or the
    # automatic startup scan + a manual one) would step on each other's
    # toes and double the runtime: if one is already running, this request
    # is ignored
    if not scan_lock.acquire(blocking=False):
        return
    try:
        set_scan_state(True, "Scan in progress. The process keeps running in the background...")
        scraper.run_scan()
    except Exception as exc:
        set_scan_state(False, f"Error during scan: {exc}")
    else:
        set_scan_state(False, "Scan completed.")
    finally:
        scan_lock.release()


def start_scheduler():
    scan_time = os.environ.get("SCAN_TIME", "07:00")
    try:
        hour, minute = scan_time.split(":")
        hour, minute = int(hour), int(minute)
    except ValueError:
        hour, minute = 7, 0
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_job, "cron", hour=hour, minute=minute)
    scheduler.start()
    print(f"Scan scheduled every day at {hour:02d}:{minute:02d}")


@app.route("/")
def index():
    data = scraper.load_data()
    sites = scraper.load_sites()

    for key, job in data["jobs"].items():
        job["_key"] = key  # template-only, never persisted

    jobs = list(data["jobs"].values())
    jobs.sort(key=lambda j: (j["site"], j["title"]))

    today = date.today().isoformat()
    visible_jobs = [j for j in jobs if not j.get("dismissed", False)]
    favorite_jobs = [j for j in visible_jobs if j.get("favorite", False)]
    new_today = [j for j in visible_jobs if j["first_seen"] == today]
    active_jobs = [j for j in visible_jobs if j.get("active", True)]
    closed_jobs = [j for j in visible_jobs if not j.get("active", True)]
    dismissed_jobs = [j for j in jobs if j.get("dismissed", False)]

    scanned_sites = [s for s in sites if s.get("scan", True)]

    spont_status = data.get("spontaneous_status", {})
    spontaneous_sites = []
    for s in sites:
        if s.get("scan", True):
            continue
        st = spont_status.get(s["id"], {})
        followup, days = scraper.needs_followup(st)
        spontaneous_sites.append({
            **s,
            "status": st.get("status", "to_apply"),
            "applied_date": st.get("applied_date"),
            "note": st.get("note", ""),
            "needs_followup": followup,
            "days_since": days,
        })
    # the ones needing a follow-up should be shown first
    spontaneous_sites.sort(key=lambda s: (not s["needs_followup"], s["name"]))

    # build enriched site_status by merging recorded checks with the configured sites
    site_status = []
    site_status_map = data.get("site_status", {})
    for s in sites:
        # spontaneous-only sites are never scanned: a status recorded in
        # the past (maybe an error) would sit there forever and confuse
        if not s.get("scan", True):
            continue
        st = site_status_map.get(s["id"], {})
        site_status.append({
            "id": s["id"],
            "name": s["name"],
            "url": s["url"],
            "count": st.get("count", 0),
            "error": st.get("error"),
            "checked_at": st.get("checked_at"),
            "curated": s.get("curated"),
        })
    # sites to check first: the ones with an error, then the ones at 0, then the rest
    site_status.sort(key=lambda s: (s.get("error") is None, s.get("count", 0) != 0, s["name"]))

    return render_template(
        "index.html",
        favorite_jobs=favorite_jobs,
        new_today=new_today,
        active_jobs=active_jobs,
        closed_jobs=closed_jobs,
        dismissed_jobs=dismissed_jobs,
        last_run=data.get("last_run"),
        scanned_count=len(scanned_sites),
        spontaneous_sites=spontaneous_sites,
        site_status=site_status,
        scan_state=get_scan_state(),
    )


@app.route("/scan", methods=["POST"])
def manual_scan():
    threading.Thread(target=run_scan_in_background, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/add-site", methods=["POST"])
def add_site_route():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    mode = request.form.get("mode", "scan")
    curated = request.form.get("curated", "").strip() or None
    if name and url:
        scraper.add_site(name, url, scan=(mode == "scan"), curated=curated)
    return redirect(url_for("index"))


@app.route("/toggle-applied", methods=["POST"])
def toggle_applied_route():
    job_key = request.form.get("job_key", "")
    scraper.toggle_job_applied(job_key)
    return redirect(url_for("index"))


@app.route("/toggle-favorite", methods=["POST"])
def toggle_favorite_route():
    job_key = request.form.get("job_key", "")
    scraper.toggle_job_favorite(job_key)
    return redirect(url_for("index"))


@app.route("/toggle-dismissed", methods=["POST"])
def toggle_dismissed_route():
    job_key = request.form.get("job_key", "")
    scraper.toggle_job_dismissed(job_key)
    return redirect(url_for("index"))


@app.route("/save-note", methods=["POST"])
def save_note_route():
    job_key = request.form.get("job_key", "")
    note = request.form.get("note", "")
    scraper.save_job_note(job_key, note)
    return redirect(url_for("index"))


@app.route("/set-spontaneous", methods=["POST"])
def set_spontaneous_route():
    site_id = request.form.get("site_id", "")
    status = request.form.get("status", "to_apply")
    scraper.set_spontaneous_status(site_id, status)
    return redirect(url_for("index"))


@app.route("/save-spontaneous-note", methods=["POST"])
def save_spontaneous_note_route():
    site_id = request.form.get("site_id", "")
    note = request.form.get("note", "")
    scraper.save_spontaneous_note(site_id, note)
    return redirect(url_for("index"))


if __name__ == "__main__":
    start_scheduler()
    if os.environ.get("RUN_ON_STARTUP", "false").lower() == "true":
        threading.Thread(target=run_scan_in_background, daemon=True).start()
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)
