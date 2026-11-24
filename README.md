# Job Bot

Local web app that finds fresh jobs that match your resume and applies to them on the company's own ATS
(Greenhouse, Lever, Ashby, Workday, and most other single-page forms), learning every answer you give it.

## Setup (Windows)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

Then double-click `start.bat` (or run `.venv\Scripts\python run.py`) and open the address it prints (it picks a free port, normally http://127.0.0.1:8770).

## First run

1. **Profile & Resume** → upload your resume (PDF/DOCX). Fill the blanks once: city/state/country, work
   authorization, `needs_sponsorship` (Yes/No), `work_authorization_answer`, EEO answers
   (or "Decline to self identify"), salary, school/degree.
2. **Settings**
   - `anthropic_api_key` (recommended): better resume parsing, answers new questions from your resume,
     and powers internet job search. Every answer Claude gives is saved in **Saved answers**, marked "AI (check it)".
   - `serper_api_key` or `brave_api_key` (optional): faster/cheaper internet search.
   - `imap_user` / `imap_password`: mailbox for verification codes and applied/rejected/interview tracking.
     Gmail needs an App Password.
   - `ats_account_password`: the password the bot uses to sign in to, or create, Workday-style accounts.
   - `auto_submit`: `true` submits automatically; `false` fills the form and waits for you to click Submit.
   - `search_keywords`, `search_locations`, `max_job_age_days`, `exclude_title_words`, `needs_sponsorship`.
3. **Dashboard → Open bot browser** and log in to jobright.ai (and any other listing site in
   `listing_urls`). This is your normal Chrome (or Edge) with its own separate "bot" profile per user, so Google
   sign-in works and logins are kept after you close it. You can keep using that window; the bot opens its own
   tabs and closes only those. Your everyday Chrome profile is never touched.
4. **Avoid companies**: companies to never apply to (your current employer, for example).

## Daily use

Dashboard → **Find jobs** → review the Jobs tab → **Apply to all new matches** (or tick jobs → Apply to selected).

- **Needs input**: questions the bot couldn't answer. Answer once and the answer is reused everywhere.
  When the list is empty, waiting jobs are retried automatically.
- **Yellow bar**: the bot is waiting for you (CAPTCHA, login, or review when auto-submit is off). Do it in the bot
  browser, then press Continue / I submitted it / Skip.
- **Read mailbox**: records application confirmations, rejections and interview invites, and marks jobs as applied.
- **+ user**: a separate profile for someone else, with their own resume, answers, settings, avoid list and browser logins.

## Use it online (phone or any computer)

The dashboard can run on **Netlify** (like PrepHub) while the applying keeps running on your PC, in your real
Chrome where you are signed in to LinkedIn, Dice and the rest. Everything you do online (apply, answer questions,
upload a resume, find jobs, read mail) is sent to your PC, which runs it and sends the result back. Pages you've
opened before still load when the PC is off; changes you make then are queued and run when the PC is back on.
Passwords and API keys never go online: they can only be changed on the PC.

```
phone / browser --password--> Netlify (dashboard + cached data + queue)  <--polls--  your PC (start.bat)
```

### 1. Deploy to Netlify (one time)

Easiest: https://app.netlify.com -> **Add new project -> Import an existing project -> GitHub** -> pick
`anand-fulltime-bot`. Leave the build command empty and the publish directory `static`. Every `git push` then
redeploys.

Or from this folder: `npm install`, `npx netlify login`, `npx netlify init`, `npx netlify deploy --prod`.

### 2. Set the password (required)

Netlify -> your site -> **Project configuration -> Environment variables -> Add a variable**: key `APP_PASSWORD`,
value a long password of your choice. Then **Deploys -> Trigger deploy**. The site refuses to work without it,
because your resume and application history are stored there.

### 3. Connect your PC

In the bot on your PC (http://127.0.0.1:8770) -> **Settings**: set `online url` to your site address
(e.g. `https://anand-jobbot.netlify.app`) and `online password` to the same `APP_PASSWORD`. Save. The header
shows "Online dashboard: connected", and the online site shows "PC worker: online".

Keep `start.bat` running (minimized is fine) whenever you want the bot to apply. On a phone, open the site and use
"Add to Home screen" to install it like an app.

Test the online site locally: create `.env` with `APP_PASSWORD=something`, run `npx netlify dev`, and point
`online url` at http://localhost:8888.

## How it works

| Part | File |
|---|---|
| Job sources: Greenhouse / Lever / Ashby / Workday public APIs, internet search to discover new companies | `app/search.py` |
| Listing-site scraping (jobright.ai, LinkedIn, …) and the browser worker | `app/browser.py` |
| Generic form filler (any ATS, iframes, react-select, Yes/No buttons, uploads) | `app/filler.py` |
| ATS flows, Greenhouse email security code, Workday account + multi-step wizard | `app/apply.py` |
| Answer memory and rules | `app/answers.py` |
| IMAP: codes, verification links, applied/rejected/interview tracking | `app/email_reader.py` |
| API + UI | `app/main.py`, `static/` |
| Online dashboard relay (Netlify function / PC side) | `netlify/functions/api.mts`, `app/relay.py` |

Data (database, resumes, browser profiles) lives in `data/` and is not committed.

Tests: `.venv\Scripts\python tests\test_filler.py` fills a local sample form end to end.
`tests\dry_scan.py <url>` shows what the bot would answer on a real form without submitting.

## Limits

- CAPTCHAs are never bypassed. The bot pauses and asks you to solve them.
- LinkedIn Easy Apply is not automated.
- Workday tenants differ a lot. When a step can't be completed, the job is marked "needs manual" with the reason.
- Some sites' terms of service forbid automated applications. Use it responsibly and keep volumes sensible.
