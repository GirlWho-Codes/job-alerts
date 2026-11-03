"""
Job alerts scraper.

Pulls remote jobs from several sources, keeps only the ones that match your
skills, are clearly open to candidates outside the US, AND were posted recently,
de-duplicates against jobs already seen, and pushes new matches to Telegram.

Config is at the top. Secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) come from
environment variables, never from this file.
"""

import os
import re
import json
import time
import html
import calendar
import pathlib
from datetime import datetime
import requests
import feedparser

# --------------------------------------------------------------------------
# CONFIG — edit these to tune what you get alerted about
# --------------------------------------------------------------------------

# A job's title must contain at least one of these (case-insensitive).
INCLUDE_KEYWORDS = [
    # LLM / AI
    "ai engineer", "ai developer", "applied ai", "ai automation", "ai agent",
    "genai", "gen ai", "llm", "prompt engineer", "machine learning", "ml engineer",
    # Automation / integration / browser automation / scraping
    "automation", "integration engineer", "workflow automation",
    "no-code", "no code", "nocode", "zapier", "n8n", "make.com", "rpa",
    "automation specialist", "solutions engineer", "forward deployed",
    "forward-deployed", "implementation engineer", "sales engineer",
    "support engineer", "developer experience", "developer advocate",
    "web scraping", "scraping", "selenium", "playwright", "apify", "crawler",
    # GTM / RevOps
    "growth engineer", "revops", "revenue operations", "marketing automation",
    # Frontend / full-stack
    "full stack", "full-stack", "fullstack", "front end", "front-end", "frontend",
    "react", "vue", "next.js", "javascript", "typescript",
    "web developer", "software engineer", "software developer",
    "founding engineer", "product engineer",
    # E-commerce / Shopify
    "shopify", "ecommerce", "e-commerce", "liquid",
    # Data / analytics
    "data engineer", "analytics engineer", "data analyst", "data analytics",
    "business intelligence", "bi developer",
]

# Hard excludes — if the text contains any of these, skip it.
# Very-senior titles and non-English language requirements.
EXCLUDE_TITLE = [
    "principal", "staff ", "vp ", "head of", "director",
    "german-speaking", "german speaking", "fluent german", "native german",
    "german required", "speak german",
    "french-speaking", "french speaking", "fluent french", "native french",
    "spanish-speaking", "fluent spanish", "native spanish",
    "dutch-speaking", "fluent dutch", "native dutch",
    "portuguese-speaking", "fluent portuguese", "japanese-speaking",
    "mandarin required", "fluent mandarin",
    "security clearance", "clearance required",
]

# Only alert on jobs posted within this many days (where the source gives a date).
MAX_AGE_DAYS = 5

# Location handling (STRICT): keep a job only if the location clearly shows it
# is open to non-US candidates. Anything that only says "Remote" or is unknown
# is dropped, because those are most often US-restricted in the fine print.
WORLDWIDE_HINTS = ["worldwide", "anywhere", "global", "anywhere in the world",
                   "\U0001F30D", "\U0001F30E", "\U0001F30F"]
NON_US_FRIENDLY = ["africa", "emea", "europe", "international", "latam",
                   "apac", "nigeria", "asia", "remote - emea"]
US_ONLY_HINTS = ["usa", "united states", "u.s.", "us only", "us-only",
                 "us remote", "remote us", "remote, us", "remote-us",
                 "(us ", "us)", "canada only", "north america",
                 "americas only", "americas", "us-based", "u.s.-based",
                 "based in the us", "based in the united states",
                 "located in the us", "must reside in the us", "green card",
                 "us citizen", "u.s. citizen", "authorized to work in the us",
                 "work authorization in the us"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SEEN_FILE = pathlib.Path(__file__).parent / "seen.json"
MAX_ALERTS_PER_RUN = 25          # safety cap so you never get flooded
HTTP_TIMEOUT = 25
UA = {"User-Agent": "job-alerts-bot/1.0 (+https://github.com/girlwho-codes/job-alerts)"}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_epoch(v):
    """Best-effort convert many date formats to epoch seconds, or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        return v / 1000.0 if v > 1e12 else v
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
        return f / 1000.0 if f > 1e12 else f
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(s[:19], fmt))
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def age_label(epoch):
    """Human 'posted' label from an epoch, or '' if unknown."""
    if not epoch:
        return ""
    days = (time.time() - epoch) / 86400
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    return f"{int(days)}d ago"


def text_excluded(s: str) -> bool:
    s = (s or "").lower()
    return any(x in s for x in EXCLUDE_TITLE)


def title_matches(title: str) -> bool:
    t = (title or "").lower()
    if text_excluded(t):
        return False
    return any(k in t for k in INCLUDE_KEYWORDS)


def location_ok(loc: str) -> bool:
    """STRICT: keep only if clearly open to non-US candidates."""
    l = (loc or "").lower()
    if any(u in l for u in US_ONLY_HINTS):
        return False
    if any(h in l for h in WORLDWIDE_HINTS):
        return True
    if any(h in l for h in NON_US_FRIENDLY):
        return True
    return False  # bare 'Remote' / unknown -> drop (usually US-restricted)


def recent_enough(job: dict) -> bool:
    d = job.get("date")
    if d is None:
        return True
    return (time.time() - d) <= MAX_AGE_DAYS * 86400


def keep(job: dict) -> bool:
    return (title_matches(job["title"]) and location_ok(job["location"])
            and recent_enough(job))


# --------------------------------------------------------------------------
# Sources — each returns a list of normalized jobs; each is isolated so one
# failing source never breaks the run.
# --------------------------------------------------------------------------

def norm(source, uid, title, company, location, url, date=None):
    return {
        "source": source,
        "uid": f"{source}:{uid}",
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
        "date": parse_epoch(date),
    }


def from_remotive():
    out = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs?limit=200",
                         headers=UA, timeout=HTTP_TIMEOUT)
        for j in r.json().get("jobs", []):
            out.append(norm("remotive", j.get("id"), j.get("title"),
                            j.get("company_name"),
                            j.get("candidate_required_location"), j.get("url"),
                            j.get("publication_date")))
    except Exception as e:
        print("remotive failed:", e)
    return out


def from_remoteok():
    out = []
    try:
        r = requests.get("https://remoteok.com/api", headers=UA, timeout=HTTP_TIMEOUT)
        for j in r.json():
            if not isinstance(j, dict) or "id" not in j:
                continue  # first element is a legal notice
            out.append(norm("remoteok", j.get("id"), j.get("position"),
                            j.get("company"), j.get("location"), j.get("url"),
                            j.get("epoch") or j.get("date")))
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
                            j.get("title"), j.get("companyName"), loc, url,
                            j.get("pubDate")))
    except Exception as e:
        print("himalayas failed:", e)
    return out


WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "https://weworkremotely.com/categories/remote-data-jobs.rss",
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
                ep = (calendar.timegm(e.published_parsed)
                      if e.get("published_parsed") else None)
                out.append(norm("wwr", e.get("id") or e.get("link"),
                                role, company, loc, e.get("link"), ep))
        except Exception as ex:
            print("wwr feed failed:", feed, ex)
    return out


def from_jobicy():
    out = []
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs?count=100",
                         headers=UA, timeout=HTTP_TIMEOUT)
        for j in r.json().get("jobs", []):
            out.append(norm("jobicy", j.get("id"), j.get("jobTitle"),
                            j.get("companyName"), j.get("jobGeo"), j.get("url"),
                            j.get("pubDate")))
    except Exception as e:
        print("jobicy failed:", e)
    return out


def from_workingnomads():
    out = []
    try:
        r = requests.get("https://www.workingnomads.com/api/exposed_jobs/",
                         headers=UA, timeout=HTTP_TIMEOUT)
        for j in r.json():
            out.append(norm("workingnomads", j.get("url") or j.get("slug"),
                            j.get("title"), j.get("company_name"),
                            j.get("location"), j.get("url"), j.get("pub_date")))
    except Exception as e:
        print("workingnomads failed:", e)
    return out


def from_hackernews():
    """Latest 'Ask HN: Who is hiring?' thread; each top-level comment ~= a posting."""
    out = []
    try:
        s = requests.get("https://hn.algolia.com/api/v1/search_by_date",
                         params={"query": "Ask HN: Who is hiring?",
                                 "tags": "story", "hitsPerPage": 1},
                         headers=UA, timeout=HTTP_TIMEOUT).json()
        hits = s.get("hits", [])
        if not hits:
            return out
        story_id = hits[0]["objectID"]
        item = requests.get(f"https://hn.algolia.com/api/v1/items/{story_id}",
                            headers=UA, timeout=HTTP_TIMEOUT).json()
        for c in (item.get("children") or [])[:300]:
            txt = c.get("text") or ""
            if not txt:
                continue
            clean = html.unescape(re.sub("<[^>]+>", " ", txt)).strip()
            low = clean.lower()
            if "remote" not in low:          # only remote postings
                continue
            if text_excluded(low):           # language / clearance requirements
                continue
            if any(u in low for u in US_ONLY_HINTS):
                continue                     # US / Americas-only -> drop
            if "worldwide" in low or "anywhere" in low or "global" in low:
                loc = "worldwide"
            elif any(h in low for h in NON_US_FRIENDLY):
                loc = "europe"               # some non-US region named
            else:
                continue                     # no clear non-US signal -> drop
            out.append(norm("hackernews", c.get("id"), clean[:200], "", loc,
                            f"https://news.ycombinator.com/item?id={c.get('id')}",
                            c.get("created_at_i")))
    except Exception as e:
        print("hackernews failed:", e)
    return out


SOURCES = [from_remotive, from_remoteok, from_himalayas, from_wwr,
           from_jobicy, from_workingnomads, from_hackernews]


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
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print("telegram send failed:", e)


def format_job(j: dict) -> str:
    esc = lambda s: html.escape(s or "")
    loc = esc(j["location"]) or "Remote"
    company = esc(j["company"])
    posted = age_label(j.get("date"))
    meta = esc(j["source"]) + (f"  ·  posted {posted}" if posted else "")
    head = f"<b>{esc(j['title'])}</b>\n"
    mid = (f"{company}  ·  {loc}\n" if company else f"{loc}\n")
    return head + mid + f"<i>{meta}</i>\n{esc(j['url'])}"


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
    new.sort(key=lambda j: j.get("date") or 0, reverse=True)  # newest first
    print(f"pulled {len(all_jobs)} jobs, {len(matches)} match your filter, "
          f"{len(new)} are new")

    for j in matches:
        seen.add(j["uid"])

    to_send = new[:MAX_ALERTS_PER_RUN]
    if first_run and to_send:
        tg_send(f"✅ Job alerts live across 7 sources. Catching you up on the "
                f"{len(to_send)} most recent matches:")
    for j in to_send:
        tg_send(format_job(j))
        time.sleep(0.5)
    if len(new) > MAX_ALERTS_PER_RUN:
        tg_send(f"...and {len(new) - MAX_ALERTS_PER_RUN} more right now. From here "
                f"on you'll just get new matches as they're posted.")

    save_seen(seen)


if __name__ == "__main__":
    main()
