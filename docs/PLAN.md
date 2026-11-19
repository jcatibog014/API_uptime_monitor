# Pulse Check — API Uptime Monitor ("Is your API still breathing?")

## Context

A new learning project in the Workspace series. The skills to practise are **scheduled jobs, metrics, alerting and failure handling**. Pulse Check checks endpoints on a schedule, records response times (split into DNS / connect / TLS / first byte), works out uptime and percentiles, opens incidents when a service really fails (not on one blip), and sends alerts that are retried until they arrive.

What the user chose:
- **Accounts:** an owner made from the PC with `create-owner`, plus a per-visitor **Try Demo** (24 h). The demo only checks built-in **practice services** and sends alerts only to a **Practice Inbox**.
- **Alerts:** Email (Brevo HTTP API), Discord / Slack webhook, Telegram bot, phone push (PWA + VAPID).
- **Extras:** Smarter checks, Status page & incidents, Alert rules, Insights.
- **Hosting:** Render free + **Supabase** free Postgres (no awake-hour limit, unlike Neon). Checks run every minute. The in-app crew also checks Pulse Check's own address, which keeps Render awake. The GitHub Actions timer is a backup wake-up.
- **Look:** "Heart monitor", drawn in CSS and SVG, not just a palette.
- **One PR with a commit per stage**, with no stops.

Names: repo https://github.com/jcatibog014/API_uptime_monitor (made by the user, public, branch `main`, not empty), cloned to a new folder `Documents\Workspace\API_uptime_monitor`, work on branch `build-pulse-check`; package `pulsecheck`, local port **5014**. This plan is copied into `docs/PLAN.md`, with a progress list and a "Where the Build Differs" list.

## Reuse from Right On Cue (`Workspace\email_campaign_scheduler\oncue`)

Port these and adapt them; don't rewrite them:
- `queue.py`: a hand-built jobs table with `enqueue` + dedupe_key, `claim` (SKIP LOCKED / guarded UPDATE), leases + `reap`, `Retry` (backoff + full jitter), `Defer`, `GiveUp`, dead letters, `retry_now`, `discard`.
- `worker.py`: rounds, sleeping until the next due moment, `nudge()`, the in-app thread, `flask worker`, and `simulate()` to play time forward in tests and the demo.
- `tick.py` + `.github/workflows/tick.yml`: POST /tasks/tick with X-Tick-Key, then tidy.
- `db.py`, `auth.py` (owner, lockout, `session_version`), `limits.py`, `tokens.py` (HMAC links), `dates.py`, `formatting.py`, `charts.py` (server SVG, measured x labels), `brevo.py`.
- `seed.py`: the demo pattern (sandbox workspace, replay through the real code, `replaying` flag, purge, max demos, `sqlite_autoincrement`, random `session_version`).
- `conftest.py`, `test_migrations.py`, `test_look.py`, `test_readme.py`, `test_demo.py`, and the `docs/take_screenshots.py` / `make_icons.py` patterns.
- AGENTS.md rules: CSP with no inline script or style, every page works without JS, toasts, Settings holds the controls, bare page titles, Title Case, no decorative emoji, README screenshots in every PR.

From other apps: Fernet `ENCRYPTION_KEY` for secrets (Hire Wire), pywebpush + VAPID + PWA (ShiroMoto), the psycopg numeric loader (Tally Ho! PR #13), and indexes on every foreign key used by bulk deletes (Tally Ho! lesson).

## Core design (the four lessons)

**Probing (`probe.py`).** Built on the standard library, with no requests or httpx, so every phase can be timed:
- resolve DNS ourselves and **check the address is public** (SSRF guard in `targets.py`: no loopback, private, link-local or metadata addresses; `ALLOW_PRIVATE_TARGETS=1` only for local runs and tests);
- connect to *that* IP, so DNS rebinding can't swap it;
- TLS with SNI, keeping the certificate's expiry;
- `http.client` request, time to first byte, and a body read capped at 64 KB;
- redirects followed up to 5 times, each hop checked again.

Every result is a `Probe` with timings and an `error_kind`: dns, refused, timeout, tls, http_status, assertion, too_slow. The prober is injectable, so tests use a local `http.server` thread, never the internet.

**Scheduled jobs (`scheduler.py`, `worker.py`).**
- Each monitor has `interval_s` (1–60 min) and `next_check_at`.
- `run_due()` claims due monitors with a compare-and-swap UPDATE and queues a `check` job with dedupe key `check:<monitor>:<scheduled_at>`.
- **No drift:** the next time is `scheduled + interval`, not `now + interval`.
- **Spread:** a stable offset per monitor (hash of its id) keeps them from all firing at :00.
- **Missed slots** (the site slept) are skipped and recorded as gaps, never as "down".
- The crew runs **several worker threads** (4 in-app), so one slow 30 s timeout doesn't hold up the others.
- `checks` has UNIQUE(monitor_id, scheduled_at). Jobs are at-least-once, so a check run twice is recorded once.
- **Check lag** (start time minus scheduled time) is recorded and shown on the Crew page.

**Failure handling (`incidents.py`).**
- States, each with a plain-English note in `pulse_words.py` like Right On Cue's `cues.py`: **Steady** (up), **Weak Pulse** (slower than its limit), **Irregular** (failed, checking again), **Flatline** (confirmed down), **Paused**, **Resting** (maintenance).
- **Confirm before alerting:** a failure schedules quick re-checks (20 s, then 40 s). Only `confirm_tries` failures in a row open an incident.
- **Recovery hysteresis:** an incident closes only after `recover_tries` successes in a row.
- **Our own network guard:** a canary (the self-check plus a known-good host) is checked first. If it also fails, results are marked "our side" and alerts are held, so a Render network blip doesn't page for every service.
- State changes go through one `incidents.move()` with a `version` column (optimistic locking), so two workers can't open two incidents.

**Metrics (`metrics.py`, `rollups.py`).**
- Raw `checks` rows are kept 14 days.
- An `hourly_rollup` job runs after each hour closes and **recomputes** that hour from raw rows. It is UNIQUE(monitor, period, start), so running it twice is harmless. A `daily_rollup` job builds day rows from the hour rows.
- Rollups store count, ok, slow, missed, sum/min/max ms and a **fixed log-bucket histogram**. Percentiles over long periods merge histograms, because averaging p95s is wrong.
- Uptime % = ok checks / checked checks. It leaves out maintenance and missed slots, and the page shows how many were left out.
- Charts are SVG drawn on the server: response time (p50 line + p95 band), phase bars (DNS / connect / TLS / wait), 90-day uptime bars, and a table view. Colours are checked with the **dataviz skill's validator** in both themes.

**Alerting (`alerts.py`, `channels.py`).**
- An incident event (down, up, reminder, escalation, ssl_expiring, flapping) creates `alerts` rows, UNIQUE(incident, channel, event, seq), and queues `deliver_alert` jobs.
- Delivery uses `queue.Retry` with backoff and jitter, honours **Retry-After** (Discord and Telegram 429s), uses per-channel rate limits, and ends in dead letters (Retry now / Discard).
- A channel failing many times in a row closes its breaker for 5 minutes.
- Webhook URLs, bot tokens and keys are encrypted with Fernet (`ENCRYPTION_KEY`).
- Every alert has a signed **Acknowledge** link (`tokens.py`) that stops reminders and escalation.
- Each channel has a Send a Test button that goes through the same queue.
- Demo channels write to the Practice Inbox, showing how each email, Discord, Telegram or push message would look.

## Data model (SQLAlchemy 2.0 + Alembic)

- `workspaces` (is_demo, replaying, time_zone, sqlite_autoincrement) and `users`
- `monitors` (kind http / tcp / heartbeat / practice, url, method, headers, body, expected_statuses, timeout_s, interval_s, next_check_at, state, state_since, confirm_tries, recover_tries, slow_ms, assertions JSON, sleepy_host, heartbeat_token, grace_s, paused, version)
- `checks`, `rollups`, `incidents`, `incident_updates`, `ssl_certs`
- `channels` (kind, name, secret_enc, settings), `monitor_channels` (level 1 / escalation level 2)
- `alerts`, `practice_inbox`, `push_subscriptions`
- `status_pages`, `status_page_monitors`, `maintenance_windows`
- `jobs`, `workers`

Every row has `workspace_id`, and every foreign key has a leading index.

## Practice services (`practice.py`)

`practice://<name>` targets are answered in-process with no network. Each has a script that depends on time plus seeded dice: "Payments API" times out for 7 minutes every few hours, "Search API" is sometimes slow, "Old Partner API" flaps, "Images CDN" serves a certificate that expires in 9 days, and the "Nightly backup" heartbeat misses one night. The script depends on the moment, so `simulate()` replays the past exactly.

## Stages (one PR, one commit per stage)

1. **Setup and probing.** Project scaffold from Right On Cue (app factory, db, Alembic, owner auth + `create-owner`, CSP, Settings with theme + Log out, toasts, AGENTS.md / CLAUDE.md, README). Heart-monitor base look. `targets.py` SSRF guard, `probe.py` with phase timings, monitor CRUD for HTTP (URL, method, interval, timeout, expected statuses), and a **Check now** button.
2. **Scheduled jobs.** Port `queue.py` and `worker.py` (multi-thread crew), `scheduler.py` (no drift, spread, missed slots), the `checks` table + dedupe, `tick.py` + workflow, `flask worker` / `tick`, the self-check, and a **Crew** page (workers, queue counts, check lag, dead letters).
3. **Failure handling + demo.** Error kinds, confirm re-checks, recovery hysteresis, incidents + `incidents.move()`, the canary guard, `pulse_words.py`, the monitor page (state, recent checks timeline, incident list), practice services, and the **Try Demo** ("Lakbay Travel", made up: ~9 services) replayed through `simulate()`. Every later stage adds its own sample data.
4. **Metrics.** Hourly and daily rollups with histograms, pruning, uptime % and p50/p95/p99, SVG charts, and the Dashboard (a tile per monitor with a live ECG sparkline, uptime and p95). The demo writes 90 days of rollups.
5. **Alerts.** Channels (Brevo email, Discord / Slack webhook, Telegram with "find my chat" via getUpdates, phone push + PWA + service worker), encryption, the alert outbox with retries / Retry-After / breaker / dead letters, Acknowledge links, Send a Test, and the Practice Inbox.
6. **Smarter checks.** Assertions (body contains / doesn't contain, JSON path equals, header present, response time limit = Weak Pulse), SSL expiry warnings at 14 / 7 / 3 days, TCP port checks, heartbeat monitors (`/hb/<token>`, grace period), and a "sleepy host" option (long first timeout for free-plan sites that cold-start).
7. **Alert rules.** Per-monitor routing, reminders every N min while down, escalation to level 2 after M min unacknowledged, flapping detection (≥5 changes in 30 min → one Flapping alert, then quiet until stable for 15 min).
8. **Status page & incidents.** Public `/status/<slug>` with 90-day bars and current incidents, incident notes (Investigating / Identified / Monitoring / Resolved) with a timeline, maintenance windows (mute alerts, shown as Resting, left out of uptime), and an RSS feed.
9. **Insights + deploy.** SLO targets, error budget and burn rate, compare periods, CSV export, and a weekly report job (Monday 8 AM in the workspace's time zone, sent to chosen channels). Also `render.yaml` (service `pulse-check`, one gunicorn process, crew in-app), `docs/DEPLOY.md` (Supabase **Session pooler** URL, because the direct address is IPv6-only; Render env; GitHub secrets TICK_URL / TICK_KEY; Brevo, Telegram, Discord, VAPID), the README (A1 English, tech table, all screenshots), and `docs/take_screenshots.py`.

## Look: Heart monitor

- **Dark ("monitor on"):** near-black screen `#07110d` with a faint green ECG grid, glowing green trace `#22c55e` for Steady, amber for Weak Pulse, red for Flatline. Big readouts like a bedside monitor ("142 ms", "99.98%").
- **Light ("clinic chart"):** cream ECG paper with a pink-red grid and a dark trace.
- The sidebar is the monitor's bezel. The header has an animated SVG ECG line that **flatlines while any monitor is down**; it is still for `prefers-reduced-motion`.
- Fonts (self-hosted Fontsource): Space Grotesk for headings, Inter for text, JetBrains Mono for readouts.
- Status colours and chart colours are checked with the dataviz validator.
- Logo: a heart-beat line inside a rounded screen. I draw a first version in SVG (`docs/make_icons.py` makes every PNG); the user may replace it with their own later.

## Cautions to tell the user

- **Render's free plan has 750 instance hours a month for the whole account.** If Pulse Check checks their other Render apps (Gawi, Tally Ho!, Hire Wire…) every few minutes, it keeps each one awake and the account runs out. For those apps: check every 60 min with "sleepy host" on, or only check Pulse Check itself often.
- Supabase free: 500 MB (raw checks are kept 14 days; at most 20 demos live), and a project pauses after 7 days with no activity. The 1-minute checks keep it active.
- ICMP ping isn't possible on Render (it needs root), so "TCP port" stands in for it.

## Verification

- `.venv\Scripts\python -m pytest`, all on SQLite with no internet. Each test uses a local `http.server` thread or the practice services. It covers: drift and missed slots, dedupe of a check run twice, two threads never claiming one monitor, confirm and recover hysteresis, the canary holding alerts, rollup recompute being idempotent, merged-histogram percentiles, the alert retry / Retry-After / dead-letter path, the SSRF guard (private IPs, rebinding, redirects to private hosts), flapping, maintenance leaving uptime out, every demo page filled, migrations matching the models, the README matching the screenshots, and the light/dark look blocks matching.
- Run locally on port 5014 with `ALLOW_PRIVATE_TARGETS=1`. Add a monitor for one of the user's local apps, stop that app, and watch Irregular → Flatline → alert in the Practice Inbox (or a real Discord webhook) → restart → Steady + "back up" alert.
- Two `flask worker` windows with `RUN_WORKER=0`: check that no monitor is checked twice for one slot.
- Try Demo: the replay fills every page (Dashboard, monitor pages, incidents, Crew, Practice Inbox, status page, Insights).
- Screenshots via Playwright, borrowed from expense_tracker's `.venv`, checked for alignment before the PR.
- Not verifiable by me: real Brevo, Telegram, push on a real phone, and Supabase + Render live. These are listed as the user's steps. The suite on a Supabase branch database is offered as a step too.

## Progress

- [x] Stage 1: setup and probing
- [x] Stage 2: scheduled jobs
- [x] Stage 3: failure handling + demo
- [x] Stage 4: metrics
- [x] Stage 5: alerts
- [x] Stage 6: smarter checks
- [x] Stage 7: alert rules
- [ ] Stage 8: status page and incidents
- [ ] Stage 9: Insights and deploy

## Where the Build Differs

- Stage 1: the `checks` table came in stage 1 (not stage 2), so Check now can save its result. It already has the `slow` and `our_side` columns that stages 3 and 6 use.
- Stage 2: the timer runs every 10 minutes (Right On Cue's ran every 30, to save Neon's awake hours; Supabase has no such limit). The queue's lease is 120 s, not 60, so a sleepy host's 90-second check never looks like a dead worker.
- Stage 3: the demo replays 3 hours, not 6, and its due checks run straight away (`direct`) instead of as queued jobs: through the queue, 6 hours took 18 s on SQLite and would take minutes on Supabase over the network. The 90 days before come from the same practice scripts in stage 4. The Payments API's demo outage has a fixed time (105 minutes before the visitor), so every demo has a finished incident. The canary's two checks are on the Crew page.
- Stage 4: rollups keep a histogram as 21 integer columns (b0..b20), not JSON, so SQL can add them up. Day rows are per workspace day (its time zone). The Payments API's practice outages come every 3 days, not every few hours, and the partner fails 6% of answers, not 17%, so 90 days look like a real service. The uptime strip has 3 levels, not 4: the status palette's orange and yellow failed the validator next to each other, and red/green fail for colour-blind readers, so bar height is the second signal.
- Stage 5: the breaker opens after 5 failures in a row (the plan said "many"). Webhook addresses must be Discord's or Slack's own, so a channel can't be used to reach a private network. Acknowledge is a page with a button, not a link that acts on a visit (mail apps and chat previews open links by themselves).
- Stage 6: certificate warnings also go out at 1 day. A heartbeat's missed check confirms at once (1 failure, 1 good ping to recover). Speed: the demo's history samples 6 answers an hour and 24 a day, the practice maths is cached, and a replay round takes every monitor due in the next minute (never past the next queued job), so the tests take under two minutes again.
- Stage 7: flapping is 3 incidents in 30 minutes (the plan said 5 state changes: an incident is 2 changes, and Irregular flickers would count too). A flapper that settles DOWN sends its held down alert instead of "settled".
