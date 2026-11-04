# job-alerts

A small, self-running pipeline that finds remote jobs I can actually apply for
and sends them to my phone within minutes of being posted.

Every 15 minutes a GitHub Action pulls fresh listings from seven job sources,
keeps only the roles that match my skills, are open to candidates outside the
US, and were posted recently, drops anything already seen, and sends each new
match to Telegram.

Built with Claude Code. Python, GitHub Actions, Telegram Bot API.

## Why I built it

I'm a remote engineer based outside the US. Most "remote" listings turn out
to be US-only once you read the fine print, so checking job boards by hand
meant reading a lot of postings I could never take. I wanted a filter that
surfaces only the roles that are genuinely open to me, as fast as possible.

## How it works

```
7 sources  ->  normalize  ->  filter  ->  dedupe  ->  Telegram
                          (skills, location,  (seen.json)
                           recency, excludes)
```

**Sources:** Remotive, RemoteOK, Himalayas, We Work Remotely (5 RSS
categories), Jobicy, Working Nomads, and the monthly Hacker News
"Who is hiring?" thread.

**Filters, in order:**
1. **Skills:** the title must match a keyword list grouped by area (AI and
   LLMs, automation and integration, front-end, e-commerce, data).
2. **Exclusions:** drops very senior titles and non-English language
   requirements (for example "German-speaking", "fluent French").
3. **Location (strict):** keeps a role only if the location explicitly says
   worldwide, anywhere, or names a non-US region. Anything that just says
   "Remote", or signals US-only ("Americas only", "US-based", "authorized
   to work in the US"), is dropped.
4. **Recency:** keeps only postings from the last 5 days, where the source
   provides a date.

**Dedupe:** every match is recorded in `seen.json`, which the Action commits
back after each run, so each job is only ever sent once.

**Alerts** include the title, company, location, source, and how long ago it
was posted.

## Design decisions

- **Precision over volume.** The first version matched 111 roles. When I
  checked them, most were US-only jobs labelled simply "Remote". I switched
  the location filter from "keep unless clearly US-only" to "keep only if
  clearly open to non-US candidates". Matches dropped to 11, and those 11
  were roles I could actually apply for. Fewer, correct alerts beat a noisy
  feed I'd stop reading.
- **Each source is isolated.** Every source runs in its own try/except. If one
  API changes or goes down, it logs an error and the other six keep working.
- **No floods.** The first run (or a reset) sends a short catch-up of the most
  recent matches, newest first, and every run is capped at 25 alerts.
- **Free text needs extra care.** Hacker News postings are unstructured, so
  the full comment is scanned for location and language requirements, not
  just the first line.
- **Secrets stay out of the code.** The bot token and chat ID live in GitHub
  Actions secrets and are read from the environment.

## Setup

1. **Create a Telegram bot:** message @BotFather, send `/newbot`, and copy the
   token. Send your new bot a message, then get your chat ID from
   @userinfobot.
2. **Add two repository secrets** (Settings -> Secrets and variables ->
   Actions): `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. **Enable Actions** and run the **job-alerts** workflow once from the
   Actions tab.

## Tuning

Everything is configured at the top of `job_alerts.py`:

- `INCLUDE_KEYWORDS`: title keywords, grouped by skill area
- `EXCLUDE_TITLE`: seniority and language requirements to drop
- `WORLDWIDE_HINTS`, `NON_US_FRIENDLY`, `US_ONLY_HINTS`: the location filter
- `MAX_AGE_DAYS`: how recent a posting must be
- `MAX_ALERTS_PER_RUN`: the per-run alert cap

The schedule is set by the `cron` line in `.github/workflows/job-alerts.yml`.
GitHub's scheduler can run a few minutes late under load, which is normal.
