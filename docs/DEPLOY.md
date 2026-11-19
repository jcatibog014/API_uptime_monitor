# Put Pulse Check Online

Pulse Check runs on **Render** (the web app) and **Supabase** (the database). Both have free plans. The steps are below, in order.

Why Supabase and not Neon (like the other apps)? Pulse Check writes a check every minute, all day. Neon's free plan counts the hours its database is awake, and that would use them all. Supabase's free database is always on.

## 1. Make the Database (Supabase)

1. Go to https://supabase.com and sign in (GitHub sign-in is fine).
2. **New project.** Name: `pulse-check`. Region: **Southeast Asia (Singapore)**. Make a strong database password and keep it in your password manager.
3. When the project is ready, press **Connect** (at the top).
4. Choose **Session pooler**. Copy the connection string. It looks like this:

   `postgresql://postgres.abcdefghij:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`

5. Put your database password in place of `[YOUR-PASSWORD]`. This is your **DATABASE_URL**. Keep it secret: never paste it into a chat or a file in the repo.

Use the **Session pooler**, not "Direct connection": the direct address only works over IPv6, and Render can't reach it.

## 2. Make the Web App (Render)

1. Go to https://render.com and sign in with GitHub.
2. **New**, then **Blueprint**. Choose the `API_uptime_monitor` repository.
3. Render reads `render.yaml` and asks for some values:
   - `DATABASE_URL`: the string from part 1.
   - `BASE_URL`: write `https://pulse-check.onrender.com` for now (part 2, step 5 fixes it if Render chose another name).
   - `BREVO_API_KEY`, `BREVO_SENDER`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`: leave them empty for now.
4. Press **Apply**. The first deploy takes a few minutes. The log ends with `Your service is live`.
5. Look at the service's address at the top of its page. If it isn't `https://pulse-check.onrender.com`, open **Environment**, change `BASE_URL` to the real address, and save (Render deploys again).

Render makes `SECRET_KEY`, `ENCRYPTION_KEY` and `TICK_KEY` by itself. Never change `SECRET_KEY` or `ENCRYPTION_KEY` later.

## 3. Make Your Login (From Your PC)

Nobody can sign up on the site: you make the owner from your PC, against the Supabase database. In PowerShell, one line at a time:

```powershell
cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
$env:DATABASE_URL = 'paste the DATABASE_URL from part 1 here'
.venv\Scripts\flask --app pulsecheck create-owner
Remove-Item Env:DATABASE_URL
```

It asks for your name, email and password. Then open the site and log in.

## 4. The Backup Timer (GitHub)

If the site restarts, it might sleep until someone visits. GitHub can call it every 10 minutes (free for public repositories):

1. On Render, open **Environment** and copy the value of `TICK_KEY`.
2. On GitHub, open the repository, then **Settings**, **Secrets and variables**, **Actions**, **New repository secret**. Make two:
   - `TICK_URL`: your site's address + `/tasks/tick`, like `https://pulse-check.onrender.com/tasks/tick`
   - `TICK_KEY`: the value from step 1.
3. Open **Actions**, choose **Pulse Check timer**, and press **Run workflow** once. It should end with a green tick.

## 5. Email Alerts (Optional, Brevo)

Render's free plan blocks normal email ports, so email goes through Brevo's web API (free: 300 emails a day).

1. Make an account at https://www.brevo.com.
2. **Senders, domains and dedicated IPs**: add your email address as a sender and confirm it (Brevo sends you a link).
3. **SMTP & API**, **API keys**: make a key.
4. On Render, set `BREVO_API_KEY` to the key and `BREVO_SENDER` to the confirmed address.

Then add an Email channel on the Alerts page and press **Send a test**.

## 6. Phone Alerts (Optional)

On your PC:

```powershell
cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
.venv\Scripts\flask --app pulsecheck make-vapid-keys
```

Copy the two lines into Render's `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY`. Then, on your phone: open the site, add it to your Home Screen (on an iPhone: Share, Add to Home Screen), open it from there, and on the Alerts page add **Phone Alerts** and press **Turn on alerts on this device**.

## Discord, Slack and Telegram

They need nothing on the server: add them on the Alerts page. Discord: the channel's settings, **Integrations**, **Webhooks**, **New Webhook**, **Copy Webhook URL**. Slack: make an app with **Incoming Webhooks**. Telegram: talk to **@BotFather**, send `/newbot`, then send your new bot any message.

## Watching Your Other Render Apps: Be Careful

Render's free plan gives your **whole account** 750 hours a month. A check counts as a visitor, so checking your other free Render apps (Gawi, Tally Ho!, Hire Wire...) every few minutes keeps them all awake, and the account runs out of hours. For those apps:

- check them **every hour**, and
- tick **It sleeps when nobody visits** (under "What a good answer looks like"): Pulse Check then waits up to 90 seconds for a sleeping app to wake.

## Run the Tests on PostgreSQL (Optional)

The tests use SQLite. To run them on PostgreSQL too, make a **second, empty** Supabase project (never the live one: every test deletes all tables), copy its Session pooler string, then:

```powershell
cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
$env:TEST_DATABASE_URL = 'the second project''s string'
.venv\Scripts\python -m pytest
Remove-Item Env:TEST_DATABASE_URL
```

Over the internet this takes a long time (maybe 30 minutes or more).
