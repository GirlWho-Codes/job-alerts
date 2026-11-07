"""
Job alerts scraper.

Pulls remote jobs from several sources, keeps only the ones that match your
skills, are open to someone based in Nigeria, AND were posted recently,
de-duplicates against jobs already seen, and pushes new matches to Telegram.

Every job goes through four checks, in this order:
  1. title   - matches one of your skill angles, and isn't a stack/role you don't do
  2. location - says worldwide / Africa / EMEA (not "Europe", "LATAM", "anywhere in the US")
  3. description - no US work-auth wall, no "must live in X", no foreign-language requirement
  4. recency - posted within MAX_AGE_DAYS

Run `python job_alerts.py --report` to print every match plus why everything
else was rejected, without sending anything or touching seen.json.

Config is at the top. Secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) come from
environment variables, never from this file.
"""

import os
import re
import sys
import json
import time
import html
import calendar
import pathlib
from collections import Counter
from datetime import datetime
import requests
import feedparser

# --------------------------------------------------------------------------
# CONFIG — edit these to tune what you get alerted about
# --------------------------------------------------------------------------

# A job's title must contain at least one of these as a whole word/phrase
# (case-insensitive). "react" won't match "Reactor", "liquid" won't match "Liquidity".
INCLUDE_KEYWORDS = [
    # LLM / AI (building with models, not training them)
    "ai engineer", "ai developer", "applied ai", "ai automation", "ai agent",
    "ai agents", "agentic", "ai solutions", "ai integration", "ai implementation",
    "genai", "gen ai", "generative ai", "llm", "prompt engineer",
    # Automation / integration / browser automation / scraping
    "automation engineer", "automation developer", "automation specialist",
    "automation consultant", "automation lead", "automation architect",
    "workflow automation", "business automation", "process automation",
    "ai automation", "marketing automation", "sales automation",
    "integration engineer", "integrations engineer", "integration developer",
    "no-code", "no code", "nocode", "low-code", "low code",
    "zapier", "n8n", "make.com", "rpa", "uipath", "power automate",
    "solutions engineer", "solution engineer", "solutions architect",
    "forward deployed", "forward-deployed", "implementation engineer",
    "implementation specialist", "technical consultant", "sales engineer",
    "support engineer", "developer advocate", "developer experience",
    "web scraping", "scraping", "scraper", "selenium", "playwright", "apify",
    "crawler", "crawling",
    # GTM / RevOps
    "growth engineer", "gtm engineer", "go-to-market engineer", "revops",
    "revenue operations", "hubspot",
    # Frontend / full-stack (JS world)
    "full stack", "full-stack", "fullstack", "front end", "front-end", "frontend",
    "react", "vue", "next.js", "nextjs", "javascript", "typescript", "node.js",
    "web developer", "software engineer", "software developer",
    "founding engineer", "product engineer", "internal tools", "retool",
    # E-commerce / Shopify
    "shopify", "liquid", "ecommerce developer", "e-commerce developer",
    "ecommerce engineer", "e-commerce engineer",
    # Data / analytics
    "data engineer", "analytics engineer", "data analyst", "data analytics",
    "business intelligence", "bi developer", "bi analyst", "reporting analyst",
    "bigquery", "tableau", "looker",
]

# Title kills: seniority you're not targeting, stacks you don't work in,
# and roles that only look similar.
EXCLUDE_TITLE_WORDS = [
    # seniority / management
    "principal", "staff", "vp", "vice president", "head of", "director",
    "engineering manager", "cto", "chief", "intern", "internship", "junior",
    "entry level", "entry-level", "graduate",
    # stacks you don't do
    "java", "golang", "go developer", "go engineer", "rust", "c++", "c#",
    ".net", "dotnet", "ios", "android", "kotlin", "swift", "flutter",
    "react native", "scala", "elixir", "ruby", "rails", "php", "laravel",
    "magento", "salesforce", "sap", "servicenow", "dynamics 365", "workday",
    "netsuite", "embedded", "firmware", "hardware", "fpga", "mainframe",
    "cobol", "unity", "unreal", "game", "blockchain", "web3", "solidity",
    "smart contract", "crypto",
    # infra / security / QA
    "devops", "sre", "site reliability", "platform engineer", "infrastructure",
    "cloud engineer", "kubernetes", "network engineer", "security engineer",
    "security", "cybersecurity", "penetration", "soc analyst",
    "qa", "quality assurance", "test automation", "automation test",
    "automation tester", "sdet", "test engineer", "tester",
    # industrial "automation"
    "plc", "scada", "industrial", "electrical", "mechanical", "controls engineer",
    "building automation", "process engineer", "manufacturing",
    # model training / research (not your lane yet)
    "machine learning engineer", "ml engineer", "mlops", "research scientist",
    "research engineer", "applied scientist", "computer vision", "deep learning",
    "phd", "data scientist", "inference", "vllm", "tensorrt", "cuda",
    # gigs that only look like engineering roles
    "interviewer", "annotator", "rater",
    # region named in the title (role is locked to that place)
    "latam", "latin america", "usa", "u.s.", "canada", "brazil", "mexico",
    "uk", "germany", "spain", "italy", "france", "poland", "india", "apac",
    # look-alike non-dev roles
    "account executive", "account manager", "sales manager", "sales representative",
    "sdr", "bdr", "recruiter", "talent", "copywriter", "content writer",
    "customer success manager", "teacher", "tutor", "trainer",
    # language requirement in the title
    "german", "french", "spanish", "dutch", "portuguese", "italian", "polish",
    "japanese", "mandarin", "korean", "swedish", "danish", "norwegian",
    "finnish", "czech", "turkish", "arabic", "hebrew", "russian",
    "bilingual",
    # clearance
    "clearance", "ts/sci",
]

# Only alert on jobs posted within this many days (where the source gives a date).
MAX_AGE_DAYS = 5

# ---- Location (STRICT, Nigeria-eligible) ---------------------------------
# Location must clearly include you. "Europe", "LATAM", "APAC", "Americas",
# single countries, bare "Remote" and blanks are dropped, because those roles
# need local work rights in that region.
OPEN_LOCATION = ["worldwide", "anywhere", "global", "globally", "international",
                 "remote - global", "work from anywhere",
                 "\U0001F30D", "\U0001F30E", "\U0001F30F"]
INCLUDES_NIGERIA = ["africa", "emea", "nigeria", "west africa", "sub-saharan",
                    "utc+1", "utc +1", "gmt+1", "wat"]
# Even if a location says "anywhere", these words mean it's really one region.
REGION_QUALIFIERS = [
    "us", "usa", "u.s.", "united states", "america", "americas", "north america",
    "canada", "latam", "latin america", "south america", "mexico", "brazil",
    "europe", "eu", "uk", "united kingdom", "germany", "france", "spain",
    "poland", "netherlands", "portugal", "italy", "ireland", "bulgaria",
    "romania", "apac", "asia", "india", "philippines",
    "australia", "new zealand", "pakistan",
]
# You work any hours, so a region named only as a time-zone overlap is fine:
# "Worldwide (overlap with US hours)" passes, "Anywhere in the US" doesn't.
TIMEZONE_WORDS = ["time zone", "timezone", "time-zone", "hours", "overlap"]

# ---- Description walls ----------------------------------------------------
# Phrases in the job text that mean you can't take it from Nigeria.
WORK_AUTH_WALLS = [
    r"\bauthori[sz]ed to work in the (us|u\.s\.|united states|uk|eu|european union|eea|canada)",
    r"\b(us|u\.s\.) (citizen|citizenship|person|work authori[sz]ation)",
    r"\bgreen card",
    r"(unable|not able|cannot|can't|do not|don't|will not|won't) (to )?(provide |offer )?(visa )?sponsor",
    r"\bno (visa )?sponsorship",
    r"\bright to work in the (uk|u\.k\.|us|eu|united kingdom)",
    r"\b(eu|eea|european) (work permit|citizen|citizenship|passport)",
    r"\b(must|need to|required to|should) (be )?(currently )?(located|based|living|reside|residing|live)"
    r" (in|within) (the )?(us|u\.s\.|usa|united states|canada|north america|americas|"
    r"europe|eu|uk|united kingdom|latam|latin america|germany|spain|poland|portugal|"
    r"india|philippines|australia)\b",
    r"\b(open|available) (only )?to (candidates|applicants|residents) (located |based )?(in|from) "
    r"(the )?(us|usa|united states|canada|latam|latin america|europe|eu|uk)\b",
    r"\b(us|u\.s\.|usa|united states|canada|latam|europe|eu|uk)[- ]based (candidates|applicants|only)",
    r"\b(usa|u\.s\.|canada|latam|europe|eu|uk)[- ]only\b",
    r"\bus[- ]only (role|position|candidates|applicants|residents)",
    r"\bw-?2\b",
    r"security clearance",
]
LANGUAGES = (r"german|french|spanish|dutch|portuguese|italian|polish|japanese|"
             r"mandarin|chinese|korean|swedish|danish|norwegian|finnish|czech|"
             r"turkish|arabic|hebrew|russian|ukrainian|romanian|hungarian|greek")
LANGUAGE_WALLS = [
    rf"\b(fluen\w*|native|proficien\w*|business[- ]level|professional[- ]level|working)"
    rf"([- ]speaker)?([- ]level)?( command)?( knowledge)?( skills)?( in| of)? ({LANGUAGES})",
    rf"({LANGUAGES})[- ](speaking|speaker|native|fluency|fluent)",
    rf"({LANGUAGES}) (and|&) english",
    rf"english (and|&) ({LANGUAGES})",
    rf"({LANGUAGES})\s*\(?(c1|c2|b2)",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SEEN_FILE = pathlib.Path(__file__).parent / "seen.json"
MAX_ALERTS_PER_RUN = 25          # safety cap so you never get flooded
HTTP_TIMEOUT = 25
UA = {"User-Agent": "job-alerts-bot/1.0 (+https://github.com/girlwho-codes/job-alerts)"}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def phrase_re(words):
    """One regex that matches any of the phrases as whole words."""
    parts = sorted((re.escape(w.lower()) for w in words), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(parts) + r")(?![a-z0-9])")


INCLUDE_RE = phrase_re(INCLUDE_KEYWORDS)
EXCLUDE_TITLE_RE = phrase_re(EXCLUDE_TITLE_WORDS)
OPEN_LOC_RE = phrase_re([w for w in OPEN_LOCATION if w.isascii()])
OPEN_LOC_EMOJI = [w for w in OPEN_LOCATION if not w.isascii()]
NIGERIA_RE = phrase_re(INCLUDES_NIGERIA)
REGION_RE = phrase_re(REGION_QUALIFIERS)
WALL_RES = [re.compile(p) for p in WORK_AUTH_WALLS]
LANG_RES = [re.compile(p) for p in LANGUAGE_WALLS]


def clean_text(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


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


# --------------------------------------------------------------------------
# The four checks. Each returns None if the job passes, or a short reason.
# --------------------------------------------------------------------------

def check_title(title):
    t = (title or "").lower()
    if not INCLUDE_RE.search(t):
        return "title: not your skills"
    m = EXCLUDE_TITLE_RE.search(t)
    if m:
        return f"title: excluded '{m.group(1)}'"
    return None


def check_location(loc):
    l = (loc or "").lower()
    if not l.strip():
        return "location: blank"
    if NIGERIA_RE.search(l):
        return None                                   # Africa / EMEA / Nigeria named
    is_open = OPEN_LOC_RE.search(l) or any(e in l for e in OPEN_LOC_EMOJI)
    if not is_open:
        return f"location: '{loc[:40]}'"
    m = REGION_RE.search(l)
    if m and not any(w in l for w in TIMEZONE_WORDS):                                             # "Anywhere in the US", "Worldwide (EU timezones)"
        return f"location: open but limited to '{m.group(1)}'"
    return None


def check_description(desc):
    d = desc or ""
    for r in WALL_RES:
        m = r.search(d)
        if m:
            return f"description: '{m.group(0)[:50]}'"
    for r in LANG_RES:
        m = r.search(d)
        if m:
            return f"language: '{m.group(0)[:40]}'"
    return None


def check_recency(job):
    d = job.get("date")
    if d is None:
        return None
    if (time.time() - d) > MAX_AGE_DAYS * 86400:
        return "too old"
    return None


def reject_reason(job):
    return (check_title(job["title"])
            or check_location(job["location"])
            or check_description(job["desc"])
            or check_recency(job))


# --------------------------------------------------------------------------
# Sources — each returns a list of normalized jobs; each is isolated so one
# failing source never breaks the run.
# --------------------------------------------------------------------------

def norm(source, uid, title, company, location, url, date=None, desc=""):
    return {
        "source": source,
        "uid": f"{source}:{uid}",
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
        "date": parse_epoch(date),
        "desc": clean_text(desc),
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
                            j.get("publication_date"), j.get("description")))
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
                            j.get("epoch") or j.get("date"), j.get("description")))
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
                            j.get("pubDate"),
                            j.get("description") or j.get("excerpt")))
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
                                role, company, loc, e.get("link"), ep,
                                e.get("summary", "")))
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
                            j.get("pubDate"),
                            j.get("jobDescription") or j.get("jobExcerpt")))
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
                            j.get("location"), j.get("url"), j.get("pub_date"),
                            j.get("description")))
    except Exception as e:
        print("workingnomads failed:", e)
    return out


def from_hackernews():
    """Latest 'Ask HN: Who is hiring?' thread; each top-level comment ~= a posting.

    HN posts have no location field, so the first line ("Company | Role | Location
    | Remote") is used as both title and location, and the whole post is the
    description that gets checked for walls.
    """
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
        for c in (item.get("children") or [])[:400]:
            txt = c.get("text") or ""
            if not txt:
                continue
            first = html.unescape(re.sub(r"<[^>]+>", " ",
                                         re.split(r"<p>", txt, 1)[0])).strip()
            if "remote" not in first.lower():
                continue                     # header line must say remote
            out.append(norm("hackernews", c.get("id"), first[:200], "", first,
                            f"https://news.ycombinator.com/item?id={c.get('id')}",
                            c.get("created_at_i"), txt))
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
    loc = esc(j["location"][:60]) or "Remote"
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


def fetch_all():
    all_jobs = []
    for src in SOURCES:
        got = src()
        print(f"{src.__name__[5:]}: {len(got)} jobs")
        all_jobs.extend(got)
        time.sleep(1)
    return all_jobs


def report(all_jobs):
    """Print what passes and why the rest fail. Sends nothing, saves nothing."""
    reasons = Counter()
    near_misses = []          # matched your skills but failed a later check
    matches = []
    for j in all_jobs:
        why = reject_reason(j)
        if why is None:
            matches.append(j)
            continue
        reasons[why.split(":")[0] if ":" in why else why] += 1
        if not why.startswith("title:"):
            near_misses.append((j, why))
    print(f"\n=== {len(matches)} MATCHES ===")
    for j in sorted(matches, key=lambda j: j.get("date") or 0, reverse=True):
        print(f"[{j['source']}] {j['title'][:80]} | {j['company']} | "
              f"{j['location'][:40]} | {age_label(j.get('date'))}")
    print("\n=== REJECTED, BY CHECK ===")
    for k, v in reasons.most_common():
        print(f"{v:5d}  {k}")
    print(f"\n=== SKILL MATCHES THAT WERE BLOCKED ({len(near_misses)}) ===")
    for j, why in near_misses[:80]:
        print(f"[{j['source']}] {j['title'][:60]} | {j['location'][:30]} -> {why}")


def main():
    all_jobs = fetch_all()

    if "--report" in sys.argv or os.environ.get("REPORT") == "1":
        report(all_jobs)
        return

    first_run = not SEEN_FILE.exists() or not load_seen()
    seen = load_seen()

    matches = [j for j in all_jobs if reject_reason(j) is None]
    new = [j for j in matches if j["uid"] not in seen]
    new.sort(key=lambda j: j.get("date") or 0, reverse=True)  # newest first
    print(f"pulled {len(all_jobs)} jobs, {len(matches)} match your filter, "
          f"{len(new)} are new")
    for j in new:
        print(f"  NEW [{j['source']}] {j['title'][:80]} | {j['location'][:40]}")

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
