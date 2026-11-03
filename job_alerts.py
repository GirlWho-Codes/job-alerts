"""
Job alerts scraper.

Pulls remote jobs from several sources, keeps only the ones that match your
skills AND are open to candidates outside the US, de-duplicates against jobs
already seen, and pushes new matches to Telegram.

Config is at the top. Secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) come from
environment variables, never from this file.
"""

import os
import json
import time
import html
import pathlib
import requests
import feedparser

# --------------------------------------------------------------------------
# CONFIG — edit these to tune what you get alerted about
# --------------------------------------------------------------------------

# A job's title must contain at least one of these (case-insensitive).
INCLUDE_KEYWORDS = [
    "ai engineer", "ai developer", "applied ai", "ai automation", "ai agent",
    "genai", "gen ai", "llm", "prompt engineer",
    "automation", "integration engineer", "workflow automation",
    "no-code", "no code", "nocode", "zapier", "n8n", "make.com", "rpa",
    "solutions engineer", "forward deployed", "forward-deployed",
    "implementation engineer", "sales engineer", "support engineer",
    "developer experience", "developer advocate",
    "growth engineer", "revops", "revenue operations", "marketing automation",
    "full stack", "full-stack", "fullstack",
    "front end", "front-end", "frontend", "react", "vue",
    "shopify", "web developer", "software engineer", "software developer",
    "data engineer", "analytics engineer", "automation specialist",
]

# Hard excludes — if the title contains any of these, skip it.
EXCLUDE_TITLE = [
    "native german", "native french", "native dutch", "native spanish",
    "security clearance", "clearance required", "principal", "staff ",
    "vp ", "head of", "director",  # drop very senior; delete if you want them
]

# Location handling. A job is kept only if location_ok() returns True.
WORLDWIDE_HINTS = ["worldwide", "anywhere", "global", "fully remote"]
NON_US_FRIENDLY = ["africa", "emea", "europe", "international", "latam",
                   "apac", "nigeria", "remote - emea"]
US_ONLY_HINTS = ["usa", "united states", "u.s.", "us only", "us-only",
                 "us remote", "remote us", "remote, us", "remote-us",
                 "(us ", "us)", "canada only", "north america"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SEEN_FILE = pathlib.Path(__file__).parent / "seen.json"
MAX_ALERTS_PER_RUN = 25          # safety cap so you never get flooded
HTTP_TIMEOUT = 25
UA = {"User-Agent": "job-alerts-bot/1.0 (+https://github.com/girlwho-codes/job-alerts)"}

# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def title_matches(title: str) -> bool:
    t = title.lower()
    if any(x in t for x in EXCLUDE_TITLE):
        return False
    return any(k in t for k in INCLUDE_KEYWORDS)


def location_ok(loc: str) -> bool:
    """True if a candidate outside the US could plausibly apply."""
    l = (loc or "").lower()
    if any(h in l for h in WORLDWIDE_HINTS):
        return True
    if any(u in l for u in US_ONLY_HINTS):
        return False
    if any(h in l for h in NON_US_FRIENDLY):
        return True
    # bare "Remote", empty, or unknown -> keep it, location is shown in the alert
    return True


def keep(job: dict) -> bool:
    return title_matches(job["title"]) and location_ok(job["location"])


# --------------------------------------------------------------------------
# Sources — each returns a list of normalized jobs; each is isolated so one
# failing source never breaks the run.
# --------------------------------------------------------------------------

def norm(source, uid, title, company, location, url):
    return {
        "source": source,
        "uid": f"{source}:{uid}",
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
    }


def from_remotive():
    out = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs?limit=200",
                         headers=UA, timeout=HTTP_TIMEOUT)
        for j in r.json().get("jobs", []):
            out.append(norm("remotive", j.get("id"), j.get("title"),
                            j.get("company_name"),
                            j.get("candidate_required_location"), j.get("url")))
    except Exception as e:
        print("remotive failed:", e)
    return out


def from_remoteok():
    out = []
    try:
        r = requests.get("https://remoteok.com/api", headers=UA, timeout=HTTP_TIMEOUT)
        data = r.json()
        for j in data:
            if not isinstance(j, dict) or "id" not in j:
                continue  # first element is a legal notice
            out.append(norm("remoteok", j.get("id"), j.get("position"),
                            j.get("company"), j.get("location"), j.get("url")))
    except Exception as e:
        print("remoteok failed:", e)
    return out


def from_himalayas():
    out = []
    try:
        r = requests.get("https://himalayas.app/jobs/api?limit=200",
                         headers=UA, timeout=HTTP_TIMEOUT)
        for j in r.json().get("jobs", []):
            loc = j.get("locationRestrictions") or j.get("locations") or []
            if isinstance(loc, list):
                loc = ", ".join(str(x) for x in loc)
            url = j.get("applicationLink") or j.get("guid") or j.get("url")
            out.append(norm("himalayas", j.get("guid") or j.get("title"),
                            j.get("title"), j.get("companyName"), loc, url))
    except Exception as e:
        print("himalayas failed:", e)
    return out


WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
]


def from_wwr():
    out = []
    for feed in WWR_FEEDS:
        try:
            d = feedparser.parse(feed)
            for e in d.entries:
                title = e.get("title", "")
                company = ""
                role = title
                if ":" in title:          # WWR titles are "Company: Role"
                    company, role = title.split(":", 1)
                loc = e.get("region", "") or ""
                out.append(norm("wwr", e.get("id") or e.get("link"),
                                role, company, loc, e.get("link")))
        except Exception as ex:
            print("wwr feed failed:", feed, ex)
    return out


SOURCES = [from_remotive, from_remoteok, from_himalayas, from_wwr]


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def tg_send(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Telegram not configured; would have sent:\n", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print("telegram send failed:", e)


def format_job(j: dict) -> str:
    esc = lambda s: html.escape(s or "")
    loc = esc(j["location"]) or "Remote (location not stated)"
    return (f"<b>{esc(j['title'])}</b>\n"
            f"{esc(j['company'])}  ·  {loc}\n"
            f"<i>{esc(j['source'])}</i>\n{esc(j['url'])}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0))


def main():
    first_run = not SEEN_FILE.exists() or not load_seen()
    seen = load_seen()

    all_jobs = []
    for src in SOURCES:
        all_jobs.extend(src())
        time.sleep(1)

    matches = [j for j in all_jobs if keep(j)]
    new = [j for j in matches if j["uid"] not in seen]
    print(f"pulled {len(all_jobs)} jobs, {len(matches)} match your filter, "
          f"{len(new)} are new")

    for j in matches:
        seen.add(j["uid"])

    if first_run:
        # Don't flood you with the entire backlog on the very first run.
        save_seen(seen)
        tg_send(f"✅ Job alerts are live. Tracking {len(matches)} matching "
                f"roles now; you'll get a message when a NEW one is posted.")
        return

    to_send = new[:MAX_ALERTS_PER_RUN]
    for j in to_send:
        tg_send(format_job(j))
        time.sleep(0.5)
    if len(new) > MAX_ALERTS_PER_RUN:
        tg_send(f"...and {len(new) - MAX_ALERTS_PER_RUN} more new matches "
                f"(capped this run).")

    save_seen(seen)


if __name__ == "__main__":
    main()
