# job-alerts

Be first to hear about remote jobs you can actually apply for.

Every 30 minutes, a GitHub Action pulls fresh listings from several remote job
boards, keeps only the ones that match your skills **and** are open to
candidates outside the US, drops anything you've already seen, and pings you on
Telegram.

## Sources
- Remotive (has a candidate-location field, great for eligibility)
- RemoteOK
- Himalayas
- We Work Remotely (RSS, several categories)

More can be added in `job_alerts.py` -> `SOURCES`.

## Setup (one time, ~5 min)

1. **Create a Telegram bot**
   - In Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
   - Copy the **token** it gives you (looks like `8012345678:AAE...`).
   - Open your new bot and send it any message (so it can message you back).
   - Message **@userinfobot** and send `/start` to get your numeric **chat ID**.

2. **Add the two secrets to this repo**
   - Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**
   - `TELEGRAM_BOT_TOKEN` = the token
   - `TELEGRAM_CHAT_ID` = your chat ID
   - (Never put these in the code.)

3. **Enable Actions**
   - Repo -> **Actions** tab -> enable workflows if prompted.
   - Open the **job-alerts** workflow -> **Run workflow** to test it now.
   - The first run just seeds what's already out there and sends one
     "alerts are live" message. After that, you only get **new** matches.

## Tuning what you hear about

Edit the top of `job_alerts.py`:
- `INCLUDE_KEYWORDS` — a job's title must contain one of these.
- `EXCLUDE_TITLE` — titles containing these are dropped (e.g. very senior roles).
- `US_ONLY_HINTS` / `WORLDWIDE_HINTS` / `NON_US_FRIENDLY` — the location filter
  that keeps only roles a non-US candidate can take.

Commit your changes and the next run uses them.

## Cadence
Runs every 30 min via cron. Make it faster by changing the `cron` in
`.github/workflows/job-alerts.yml` (e.g. `*/15 * * * *`). GitHub's scheduler can
lag a few minutes under load, which is normal.

## Notes
- `seen.json` is updated and committed by the Action each run — that's how it
  remembers what it already sent. Don't delete it unless you want a reset.
- If a source changes its API, that source just logs an error and the others
  keep working.
