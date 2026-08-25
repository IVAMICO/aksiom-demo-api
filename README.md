# aksiom-demo-api

Tiny standalone service that receives "Request a Demo" submissions from
aksiom-website, and a small internal dashboard for tracking them (assign to
a team member, mark status, see upcoming requested call times). Separate
from the real `aksiom_tp` product on purpose — this never touches customer
financial data, so it can stay simple and public.

## What it does

- `POST /api/demo-requests` — validates the submission, checks a honeypot
  field, rate-limits by IP (5/hour), stores it.
- `GET /api/demo-requests` — returns all stored submissions as JSON. Requires
  the `X-Admin-Token` header to match `ADMIN_TOKEN`.
- `PATCH /api/demo-requests/{id}` — update a submission's `status` or
  `assigned_to`. Same token required. Sends an email to the newly-assigned
  person if SMTP is configured (see below).
- `GET /api/admin?token=...` — the actual dashboard: status/owner counts,
  upcoming requested call times, filterable table with inline-editable
  status and owner dropdowns.
- `GET /api/health` — for uptime checks.

Storage is SQLite (`data/demo_requests.db`) — no separate database server
needed, just a file, but supports real updates/filtering unlike the old
JSONL approach.

## Local development

```bash
uv sync
export ADMIN_TOKEN=some-local-secret
uv run uvicorn main:app --reload --port 8090
```

`aksiom-website`'s `vite.config.js` already proxies `/api/*` to
`localhost:8090` in dev, so running both side by side just works — no CORS
setup needed locally.

## Deploying to the EC2 box

See `aksiom-website`'s deploy flow — same server, same nginx config. To
update this service specifically after pushing changes to GitHub:

```bash
cd ~/aksiom-demo-api && git pull && uv sync && sudo systemctl restart aksiom-demo-api
```

Initial setup (first time only) is documented in the deploy history; the
short version: `uv sync`, a systemd unit running
`uv run uvicorn main:app --host 127.0.0.1 --port 8090`, and an nginx
`location /api/ { proxy_pass http://127.0.0.1:8090/api/; }` block on the
`aksiom.ai` server.

## Email notifications on assignment

Optional — assignment works fine without it, it just won't send an email
until these are set. Since Aksiom already uses Google Workspace, the
simplest path is a Gmail account with an **app password** (a regular
password won't work for this):

1. Go to your Google Account → **Security** → turn on **2-Step Verification**
   if it isn't already on
2. Go to **App passwords** (search for it in your Google Account settings),
   create one named "Aksiom demo API"
3. Add these to the systemd service's environment (same place `ADMIN_TOKEN`
   is set), then `sudo systemctl restart aksiom-demo-api`:
   ```
   Environment=SMTP_USER=info@aksiom.ai
   Environment=SMTP_PASSWORD=<the 16-character app password>
   Environment=SMTP_FROM=info@aksiom.ai
   ```

## Viewing / managing leads

Bookmark: `https://aksiom.ai/api/admin?token=<your ADMIN_TOKEN>`

That's the dashboard — assign requests to Ivana/Vidak/Milo/Vuk, mark status
(New / Contacted / Demo Scheduled / Demo Completed / Closed Won / Closed
Lost), filter by owner or status, see upcoming requested call times.

## Known limitation

Rate limiting is in-memory and resets on restart/deploy — fine at this
volume, but worth knowing if abuse ever becomes a real concern.
