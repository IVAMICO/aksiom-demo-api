import html
import os
import smtplib
import sqlite3
import time
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, field_validator

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "demo_requests.db"

# Set this in the environment before deploying — the endpoints below refuse
# all requests until it's set to a real value.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://aksiom.ai,http://localhost:5180,http://localhost:5173",
).split(",")

# Email is optional — until SMTP_USER/SMTP_PASSWORD are set, assignment still
# works, it just skips sending the notification (see send_assignment_email).
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
ADMIN_URL = os.environ.get("ADMIN_URL", "https://aksiom.ai/api/admin")

# Where the team is — preferred call times get converted to this so nobody
# has to do the timezone math by hand when a US/APAC lead picks their own slot.
TEAM_TIMEZONE = os.environ.get("TEAM_TIMEZONE", "Europe/Copenhagen")

RATE_LIMIT_WINDOW_SECONDS = 3600
RATE_LIMIT_MAX_PER_WINDOW = 5

TEAM = {
    "Ivana": "ivana@aksiom.ai",
    "Vidak": "vidak@aksiom.ai",
    "Milo": "milo@aksiom.ai",
    "Vuk": "vuk@aksiom.ai",
}

STATUSES = ["New", "Contacted", "Demo Scheduled", "Demo Completed", "Closed Won", "Closed Lost"]

app = FastAPI(title="Aksiom Demo Request API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

# In-memory per-IP rate limiting. Fine for this volume; resets on restart.
_rate_limit_state: dict[str, list[float]] = {}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS demo_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            ip TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            company TEXT NOT NULL,
            country TEXT DEFAULT '',
            timezone TEXT DEFAULT '',
            team_size TEXT DEFAULT '',
            erp_systems TEXT DEFAULT '',
            entities TEXT DEFAULT '',
            preferred_date TEXT DEFAULT '',
            preferred_time TEXT DEFAULT '',
            message TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'New',
            assigned_to TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


init_db()


class DemoRequest(BaseModel):
    name: str
    email: EmailStr
    company: str
    country: str = ""
    timezone: str = ""
    teamSize: str = ""
    erpCount: str = ""
    entityCount: str = ""
    preferredDate: str = ""
    preferredTime: str = ""
    message: str = ""
    website: str = ""  # honeypot field — real users never see or fill this

    @field_validator("name", "company")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class UpdateRequest(BaseModel):
    status: str | None = None
    assigned_to: str | None = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, v: str | None) -> str | None:
        if v is not None and v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        return v

    @field_validator("assigned_to")
    @classmethod
    def valid_assignee(cls, v: str | None) -> str | None:
        if v is not None and v != "" and v not in TEAM:
            raise ValueError(f"assigned_to must be one of {list(TEAM)} or empty")
        return v


def check_rate_limit(ip: str) -> None:
    now = time.time()
    recent = [t for t in _rate_limit_state.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(recent) >= RATE_LIMIT_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    recent.append(now)
    _rate_limit_state[ip] = recent


def check_admin_token(token: str) -> None:
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def team_local_dt(record: dict) -> datetime | None:
    """The visitor's preferred call time, converted to TEAM_TIMEZONE. None if
    there isn't enough info (missing date/time, or an unrecognized timezone)."""
    if not (record["preferred_date"] and record["preferred_time"] and record["timezone"]):
        return None
    try:
        visitor_dt = datetime.fromisoformat(
            f"{record['preferred_date']}T{record['preferred_time']}"
        ).replace(tzinfo=ZoneInfo(record["timezone"]))
        return visitor_dt.astimezone(ZoneInfo(TEAM_TIMEZONE))
    except Exception:
        return None


def format_visitor_time(record: dict) -> str:
    """The customer's preferred slot exactly as they picked it, e.g. 'Tue 1 Sep, 14:30'."""
    date_, time_ = record["preferred_date"], record["preferred_time"]
    if not (date_ and time_):
        return ""
    try:
        return datetime.fromisoformat(f"{date_}T{time_}").strftime("%a %-d %b, %H:%M")
    except Exception:
        return f"{date_} {time_}".strip()


def format_team_time(record: dict) -> str:
    """The same slot converted to TEAM_TIMEZONE, e.g. 'Wed 2 Sep, 02:00 (next day)'.
    Empty if we don't have enough to compute one (missing date/time, or an
    unrecognized timezone) — the customer's own time still stands on its own."""
    team_dt = team_local_dt(record)
    if team_dt is None:
        return ""
    label = team_dt.strftime("%a %-d %b, %H:%M")
    team_date = team_dt.date().isoformat()
    if team_date > record["preferred_date"]:
        label += " (next day)"
    elif team_date < record["preferred_date"]:
        label += " (previous day)"
    return label


def send_assignment_email(assignee_name: str, record: dict) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        return  # not configured yet — assignment still works, just no email
    to_email = TEAM.get(assignee_name)
    if not to_email:
        return

    body = (
        f"Hi {assignee_name},\n\n"
        f"A demo request has been assigned to you:\n\n"
        f"Name: {record['name']}\n"
        f"Email: {record['email']}\n"
        f"Company: {record['company']}\n"
        f"Country: {record['country']}\n"
        f"Customer's preferred time: {format_visitor_time(record) or '(not specified)'}\n"
        f"Your time (Copenhagen): {format_team_time(record) or 'n/a — missing timezone or time'}\n"
        f"Message: {record['message']}\n\n"
        f"View all requests: {ADMIN_URL}?token={ADMIN_TOKEN}\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Demo request assigned to you: {record['company']}"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    except Exception:
        pass  # never let an email hiccup break the assignment itself


@app.post("/api/demo-requests")
async def create_demo_request(payload: DemoRequest, request: Request):
    if payload.website:
        # Honeypot tripped — return success so the bot doesn't learn anything, but drop it.
        return {"status": "received"}

    # Behind nginx, request.client.host is always the proxy's own address (127.0.0.1),
    # not the real visitor — that made every visitor share one rate-limit bucket.
    # X-Real-IP (set in the nginx config) carries the actual client IP.
    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    check_rate_limit(client_ip)

    conn = get_db()
    conn.execute(
        """INSERT INTO demo_requests
           (received_at, ip, name, email, company, country, timezone, team_size,
            erp_systems, entities, preferred_date, preferred_time, message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(), client_ip, payload.name, payload.email,
            payload.company, payload.country, payload.timezone, payload.teamSize,
            payload.erpCount, payload.entityCount, payload.preferredDate,
            payload.preferredTime, payload.message,
        ),
    )
    conn.commit()
    conn.close()

    return {"status": "received"}


@app.get("/api/demo-requests")
async def list_demo_requests(x_admin_token: str = Header(default="")):
    check_admin_token(x_admin_token)
    conn = get_db()
    rows = conn.execute("SELECT * FROM demo_requests ORDER BY id DESC").fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows]}


@app.patch("/api/demo-requests/{request_id}")
async def update_demo_request(request_id: int, payload: UpdateRequest, x_admin_token: str = Header(default="")):
    check_admin_token(x_admin_token)

    conn = get_db()
    existing = conn.execute("SELECT * FROM demo_requests WHERE id = ?", (request_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="Not found")

    updates = {}
    if payload.status is not None:
        updates["status"] = payload.status
    if payload.assigned_to is not None:
        updates["assigned_to"] = payload.assigned_to

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE demo_requests SET {set_clause} WHERE id = ?", (*updates.values(), request_id))
        conn.commit()

    newly_assigned = (
        payload.assigned_to
        and payload.assigned_to != existing["assigned_to"]
        and payload.assigned_to in TEAM
    )
    if newly_assigned:
        updated = conn.execute("SELECT * FROM demo_requests WHERE id = ?", (request_id,)).fetchone()
        send_assignment_email(payload.assigned_to, dict(updated))

    conn.close()
    return {"status": "updated"}


@app.get("/api/admin", response_class=HTMLResponse)
async def admin_view(token: str = "", assignee: str = "", status: str = ""):
    if token != ADMIN_TOKEN or not ADMIN_TOKEN:
        return HTMLResponse(
            "<p style='font-family:sans-serif;padding:2rem'>"
            "Missing or incorrect token. Add <code>?token=YOUR_TOKEN</code> to the URL.</p>",
            status_code=401,
        )

    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM demo_requests ORDER BY id DESC").fetchall()]
    conn.close()

    # --- dashboard stats ---
    status_counts = {s: 0 for s in STATUSES}
    assignee_counts = {name: 0 for name in TEAM}
    unassigned_count = 0
    for r in rows:
        if r["status"] in status_counts:
            status_counts[r["status"]] += 1
        if r["assigned_to"] in assignee_counts:
            assignee_counts[r["assigned_to"]] += 1
        if not r["assigned_to"]:
            unassigned_count += 1

    # Sort/filter "upcoming" by the actual instant in the team's timezone when we
    # can compute one — a US evening slot can land on a different calendar day
    # for us, so comparing raw visitor-local date strings would misorder things.
    team_today = datetime.now(ZoneInfo(TEAM_TIMEZONE)).date().isoformat()

    def upcoming_sort_key(r):
        team_dt = team_local_dt(r)
        if team_dt is not None:
            return (0, team_dt.isoformat())
        return (1, r["preferred_date"], r["preferred_time"] or "99:99")

    def is_upcoming(r):
        if not r["preferred_date"] or r["status"] in ("Closed Won", "Closed Lost"):
            return False
        team_dt = team_local_dt(r)
        reference_date = team_dt.date().isoformat() if team_dt is not None else r["preferred_date"]
        return reference_date >= team_today

    upcoming = sorted((r for r in rows if is_upcoming(r)), key=upcoming_sort_key)[:8]

    # --- filters ---
    visible = rows
    if assignee:
        visible = [r for r in visible if r["assigned_to"] == assignee]
    if status:
        visible = [r for r in visible if r["status"] == status]

    def qs(**overrides):
        params = {"token": token, "assignee": assignee, "status": status}
        params.update(overrides)
        return "&".join(f"{k}={html.escape(v)}" for k, v in params.items() if v)

    def esc(v) -> str:
        return html.escape(str(v)) if v else "—"

    status_options_html = "".join(f'<option value="{s}">{s}</option>' for s in STATUSES)
    assignee_options_html = "".join(f'<option value="{a}">{a}</option>' for a in TEAM)

    def row_html(r: dict) -> str:
        status_opts = "".join(
            f'<option value="{s}" {"selected" if r["status"] == s else ""}>{s}</option>' for s in STATUSES
        )
        assignee_opts = '<option value="">Unassigned</option>' + "".join(
            f'<option value="{a}" {"selected" if r["assigned_to"] == a else ""}>{a}</option>' for a in TEAM
        )
        stale = r["status"] == "New" and (datetime.now(timezone.utc) - datetime.fromisoformat(r["received_at"])).days >= 2
        row_class = "stale" if stale else ""
        team_time = format_team_time(r)
        team_time_class = "day-shift" if ("next day" in team_time or "previous day" in team_time) else ""
        return f"""
        <tr class="{row_class}">
          <td>{esc(r['received_at'][:10])}</td>
          <td>{esc(r['name'])}</td>
          <td>{esc(r['email'])}</td>
          <td>{esc(r['company'])}</td>
          <td>{esc(r['country'])}</td>
          <td>{esc(format_visitor_time(r))}</td>
          <td class="{team_time_class}">{esc(team_time)}</td>
          <td>{esc(r['message'])}</td>
          <td><select onchange="updateField({r['id']}, 'status', this.value)">{status_opts}</select></td>
          <td><select onchange="updateField({r['id']}, 'assigned_to', this.value)">{assignee_opts}</select></td>
        </tr>"""

    rows_html = "".join(row_html(r) for r in visible) or '<tr><td colspan="10" class="empty">No requests match this filter.</td></tr>'

    status_badges = "".join(
        f'<a class="badge {"active" if status == s else ""}" href="/api/admin?{qs(status="" if status == s else s)}">{s}: {status_counts[s]}</a>'
        for s in STATUSES
    )
    assignee_badges = "".join(
        f'<a class="badge {"active" if assignee == a else ""}" href="/api/admin?{qs(assignee="" if assignee == a else a)}">{a}: {assignee_counts[a]}</a>'
        for a in TEAM
    )

    upcoming_html = "".join(
        f'<li><strong>{esc(format_team_time(r) or format_visitor_time(r))}</strong> — {esc(r["name"])} ({esc(r["company"])}) '
        f'<span class="muted">customer: {esc(format_visitor_time(r))} · '
        f'{esc(r["assigned_to"]) if r["assigned_to"] else "unassigned"}</span></li>'
        for r in upcoming
    ) or '<li class="muted">Nothing scheduled.</li>'

    clear_filters = '<a class="clear" href="/api/admin?token=' + html.escape(token) + '">Clear filters</a>' if (assignee or status) else ''

    page = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Demo Requests — Aksiom</title>
      <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, sans-serif; background: #0B0D11; color: #E6E8EC; padding: 2rem; margin: 0; }}
        h1 {{ font-size: 1.25rem; margin: 0 0 1.5rem; }}
        h2 {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: #8B92A0; margin: 0 0 0.75rem; }}
        .dashboard {{ display: grid; grid-template-columns: 2fr 2fr 1.3fr; gap: 1.5rem; margin-bottom: 2rem; }}
        .panel {{ background: #13161C; border: 1px solid #2A2F39; border-radius: 10px; padding: 1rem 1.25rem; }}
        .badge {{ display: inline-block; margin: 0 0.4rem 0.4rem 0; padding: 0.3rem 0.6rem; border-radius: 6px;
                  background: #1F2229; color: #C8CDD5; text-decoration: none; font-size: 0.75rem; border: 1px solid #2A2F39; }}
        .badge.active {{ background: #0F1A2A; border-color: #2D5A9E; color: #5BA1F0; }}
        .badge:hover {{ border-color: #5A6170; }}
        .clear {{ color: #5BA1F0; font-size: 0.75rem; text-decoration: none; }}
        ul {{ list-style: none; padding: 0; margin: 0; font-size: 0.8rem; }}
        li {{ padding: 0.35rem 0; border-bottom: 1px solid #1F2229; }}
        li:last-child {{ border-bottom: none; }}
        .muted {{ color: #5A6170; }}
        table {{ border-collapse: collapse; width: 100%; font-size: 0.78rem; }}
        th, td {{ text-align: left; padding: 0.55rem 0.65rem; border-bottom: 1px solid #1F2229; white-space: nowrap; }}
        th {{ color: #8B92A0; font-weight: 600; text-transform: uppercase; font-size: 0.62rem; letter-spacing: 0.05em; }}
        tr:hover td {{ background: #13161C; }}
        tr.stale td {{ background: rgba(232, 178, 90, 0.05); }}
        tr.stale td:first-child {{ box-shadow: inset 3px 0 0 #E8B25A; }}
        td.day-shift {{ color: #E8B25A; font-weight: 600; }}
        td {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; color: #C8CDD5; }}
        td.empty {{ color: #5A6170; text-align: center; padding: 2rem; }}
        select {{ background: #0B0D11; color: #E6E8EC; border: 1px solid #2A2F39; border-radius: 4px; padding: 0.3rem 0.4rem; font-size: 0.75rem; }}
      </style>
    </head>
    <body>
      <h1>Demo Requests</h1>

      <div class="dashboard">
        <div class="panel">
          <h2>By status</h2>
          {status_badges}
        </div>
        <div class="panel">
          <h2>By owner {f'<span class="muted">· {unassigned_count} unassigned</span>' if unassigned_count else ''}</h2>
          {assignee_badges}
        </div>
        <div class="panel">
          <h2>Upcoming requested calls</h2>
          <ul>{upcoming_html}</ul>
        </div>
      </div>

      {clear_filters}
      <table>
        <thead><tr>
          <th>Received</th><th>Name</th><th>Email</th><th>Company</th><th>Country</th>
          <th>Customer's Time</th><th>Our Time (CPH)</th><th>Message</th><th>Status</th><th>Owner</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>

      <script>
        const TOKEN = {token!r};
        async function updateField(id, field, value) {{
          await fetch(`/api/demo-requests/${{id}}`, {{
            method: 'PATCH',
            headers: {{ 'Content-Type': 'application/json', 'X-Admin-Token': TOKEN }},
            body: JSON.stringify({{ [field]: value }}),
          }});
          location.reload();
        }}
      </script>
    </body>
    </html>
    """
    return HTMLResponse(page)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
