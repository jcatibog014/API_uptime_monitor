# Pulse Check

**Is your API still breathing?**

Pulse Check watches your APIs and websites. It checks each one again and again, on a timetable. It times every part of the answer. When a service stops working, it tells you: by email, Discord, Slack, Telegram or a phone notification. When the service works again, it tells you that too.

It looks like a heart monitor in a hospital: a green line that beats while everything is fine, and goes flat and red when something is down.

![The Dashboard](docs/screenshots/dashboard.png)

## Try It

Open the site and press **Try the demo**. You get your own pretend company, "Lakbay Travel", for 24 hours. Its monitors check practice services inside Pulse Check, never real websites, and its alerts go to a Practice Inbox. Its last 3 hours are played again, check by check, so every page has something to show.

![The login page](docs/screenshots/login.png)

## What It Does

### Checks Every Minute

A **monitor** is one thing to check: a web address, a server port, or a job that must call Pulse Check (a heartbeat). Each monitor has its own timetable. A crew of workers makes the checks in the background.

![Every monitor](docs/screenshots/monitors.png)

Each check is timed in parts: finding the server (DNS), connecting, the secure handshake (TLS), and waiting for the answer.

![One monitor](docs/screenshots/monitor.png)

You can say what a good answer looks like: words it must have, a JSON value, a header, or a speed limit. A slow answer is a **Weak Pulse**.

![Adding a monitor](docs/screenshots/add-monitor.png)

A **heartbeat** monitor is for jobs, like a nightly backup. The job calls Pulse Check when it finishes. No call in time means trouble.

![A heartbeat monitor](docs/screenshots/heartbeat.png)

### Down Only When It Is Really Down

One failed check is not an outage. Pulse Check checks again after 20 seconds and 40 seconds. Only 3 failures in a row open an **incident**. Two good checks in a row close it. If Pulse Check's own internet connection fails, it doesn't blame your service.

![Incidents](docs/screenshots/incidents.png)

An incident page tells the whole story: when it started, the alerts that went out, your notes, and every check.

![One incident](docs/screenshots/incident.png)

### Alerts That Arrive

Alerts go to **channels**: email (Brevo), Discord, Slack, Telegram, or phones. If a service says "too many, wait", Pulse Check waits and tries again. If something can never work (like a deleted webhook), it stops and tells you.

![Alert channels](docs/screenshots/alerts.png)

![Alerts sent](docs/screenshots/alerts-sent.png)

The demo's alerts go to the **Practice Inbox**. It shows each alert the way the real service would.

![The Practice Inbox](docs/screenshots/practice-inbox.png)

**Alert rules**: choose who hears about each monitor, send reminders until someone presses "I'm on it", and escalate to someone else if nobody does. A monitor that goes down and up again and again (**flapping**) sends one alert, not fifty.

![Alert rules](docs/screenshots/alert-rules.png)

### Numbers and Charts

Uptime, the typical response time, and the slowest 5% and 1%, for 24 hours, 7, 30 or 90 days. One bar per day shows the last 90 days. A shorter bar is a worse day.

![90 days of one monitor](docs/screenshots/monitor-90-days.png)

**Insights** compare every monitor with the period before. Each monitor has an uptime goal and an **error budget**: how much failure the goal allows.

![Insights](docs/screenshots/insights.png)

### A Public Status Page

Show the people who use your services if they work, with your notes during an incident. No login needed. There is an RSS feed too.

![A status page](docs/screenshots/status-page.png)

Choose its address and which monitors it shows. Drag the rows to set their order.

![Changing a status page](docs/screenshots/status-page-form.png)

Plan **maintenance**: during the work, monitors rest. No alerts, and it doesn't count against uptime.

![Maintenance](docs/screenshots/maintenance.png)

### The Crew and Settings

The Crew page shows the workers, how late the checks are, and the jobs that gave up.

![The crew](docs/screenshots/crew.png)

![Settings](docs/screenshots/settings.png)

### Dark Mode and Phones

![The Dashboard in dark mode](docs/screenshots/dashboard-dark.png)

![A monitor in dark mode](docs/screenshots/monitor-dark.png)

<p>
  <img src="docs/screenshots/phone-login.png" alt="The login page on a phone" width="300">
  <img src="docs/screenshots/phone-dashboard.png" alt="The Dashboard on a phone" width="300">
  <img src="docs/screenshots/phone-status-page.png" alt="A status page on a phone" width="300">
</p>

<p>
  <img src="docs/screenshots/phone-incidents.png" alt="Incidents on a phone, grouped by monitor" width="300">
  <img src="docs/screenshots/phone-alerts.png" alt="Alert channels on a phone" width="300">
</p>

## How It Is Made

| Part | What it is | What it does here |
|---|---|---|
| Python 3.13 | A programming language | All of the app |
| Flask | A small web framework | The pages and the web addresses |
| SQLAlchemy | A way to talk to databases | The tables (monitors, checks, incidents...) |
| Alembic | Database changes | Changes the tables without losing data |
| SQLite | A database in one file | On your PC and in the tests |
| PostgreSQL (Supabase) | A database server | On the live site |
| A job queue in the database | Written by hand | Checks and alerts in the background, tried again when they fail |
| socket, ssl, http.client | Parts of Python | Make each check and time every part |
| cryptography | A library | Keeps webhook addresses and tokens secret |
| pywebpush | A library | Phone notifications |
| HTML, CSS, a little JavaScript | Web languages | The pages; every page works without JavaScript |
| SVG | Pictures made of code | The charts, the heartbeat line and the logo |
| pytest | Tests | Over 200 tests check the app |
| Render | A hosting platform | Runs the live site |
| GitHub Actions | Automation | Wakes the site every 10 minutes, just in case |
| Brevo | An email service | Sends email alerts |

## Run It on Your PC

```powershell
cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\flask --app pulsecheck upgrade-db
.venv\Scripts\flask --app pulsecheck create-owner
.venv\Scripts\flask --app pulsecheck run --debug --port 5014
```

Then open http://127.0.0.1:5014.

To watch your other apps on this PC (on 127.0.0.1), run this first, in the same window:

```powershell
$env:ALLOW_PRIVATE_TARGETS = '1'
```

## Test It

```powershell
.venv\Scripts\python -m pytest
```

The tests never use the internet. They check a little web server that the tests start themselves.

## Put It Online

The steps are in [docs/DEPLOY.md](docs/DEPLOY.md). The plan for the whole app is in [docs/PLAN.md](docs/PLAN.md).
