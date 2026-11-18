# Pulse Check

**Is your API still breathing?**

Pulse Check is an uptime monitor for APIs and websites. You give it an address. It checks the address again and again, and it times every part of the answer. When a service stops working, it tells you.

This app is still being built. The plan is in [docs/PLAN.md](docs/PLAN.md).

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

## Test It

```powershell
.venv\Scripts\python -m pytest
```
