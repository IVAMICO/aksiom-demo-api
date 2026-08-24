# aksiom-demo-api

Tiny standalone service that receives "Request a Demo" submissions from
aksiom-website. Separate from the real `aksiom_tp` product on purpose — this
never touches customer financial data, so it can stay simple and public.

## What it does

- `POST /api/demo-requests` — validates the submission, checks a honeypot
  field, rate-limits by IP (5/hour), appends it to a local JSONL file.
- `GET /api/demo-requests` — returns all stored submissions, but only if the
  `X-Admin-Token` header matches the `ADMIN_TOKEN` environment variable.
- `GET /api/health` — for uptime checks.

No database — submissions live in `data/demo_requests.jsonl`, one JSON object
per line. Simple to `cat`/`tail`/`grep` over SSH, or fetch via the GET
endpoint above.

## Local development

```bash
uv sync
export ADMIN_TOKEN=some-local-secret
uv run uvicorn main:app --reload --port 8090
```

`aksiom-website`'s `vite.config.js` already proxies `/api/*` to
`localhost:8090` in dev, so running both side by side just works — no CORS
setup needed locally.

## Deploying to the EC2 box (same box that runs aksiom_tp)

1. **Copy the project over**, same pattern as `aksiom_tp/deploy.sh`:
   ```bash
   rsync -az --delete -e "ssh -i ~/.ssh/aksiom-key.pem" \
     ~/projects/aksiom-demo-api/ ubuntu@18.195.138.117:~/aksiom-demo-api/ \
     --exclude .venv --exclude data
   ```

2. **On the box**, install deps and pick a real admin token:
   ```bash
   cd ~/aksiom-demo-api
   uv sync
   openssl rand -hex 24   # use this as your ADMIN_TOKEN — save it somewhere safe
   ```

3. **Create a systemd service** at `/etc/systemd/system/aksiom-demo-api.service`:
   ```ini
   [Unit]
   Description=Aksiom demo request API
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/aksiom-demo-api
   Environment=ADMIN_TOKEN=<paste the token from step 2>
   Environment=ALLOWED_ORIGINS=https://aksiom.ai
   Environment=DATA_DIR=/home/ubuntu/aksiom-demo-api/data
   ExecStart=/home/ubuntu/.local/bin/uv run uvicorn main:app --host 127.0.0.1 --port 8090
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   Then:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now aksiom-demo-api
   ```

4. **Add an nginx location block** to the `aksiom.ai` server block (the one
   serving the marketing site), so the frontend's same-origin `/api/...`
   fetch reaches this service — no CORS needed since it's the same domain:
   ```nginx
   location /api/ {
       proxy_pass http://127.0.0.1:8090/api/;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
   }
   ```
   Then `sudo nginx -t && sudo systemctl reload nginx`.

5. **Check it's alive**: `curl https://aksiom.ai/api/health` should return
   `{"status":"ok"}`.

## Viewing submitted leads

```bash
curl https://aksiom.ai/api/demo-requests -H "X-Admin-Token: <your token>"
```

Or just SSH in and `cat ~/aksiom-demo-api/data/demo_requests.jsonl`.

## Known limitation

Rate limiting is in-memory and resets on restart/deploy — fine at this
volume, but worth knowing if abuse ever becomes a real concern.
