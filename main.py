import html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, field_validator

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "demo_requests.jsonl"

# Set this in the environment before deploying — the endpoints below refuse
# all requests until it's set to a real value.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://aksiom.ai,http://localhost:5180,http://localhost:5173",
).split(",")

RATE_LIMIT_WINDOW_SECONDS = 3600
RATE_LIMIT_MAX_PER_WINDOW = 5

app = FastAPI(title="Aksiom Demo Request API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# In-memory per-IP rate limiting. Fine for this volume; resets on restart.
_rate_limit_state: dict[str, list[float]] = {}


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


def check_rate_limit(ip: str) -> None:
    now = time.time()
    recent = [t for t in _rate_limit_state.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(recent) >= RATE_LIMIT_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    recent.append(now)
    _rate_limit_state[ip] = recent


def load_records() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    records = [json.loads(line) for line in DATA_FILE.read_text().splitlines() if line.strip()]
    records.reverse()
    return records


def check_admin_token(token: str) -> None:
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/api/demo-requests")
async def create_demo_request(payload: DemoRequest, request: Request):
    if payload.website:
        # Honeypot tripped — return success so the bot doesn't learn anything, but drop it.
        return {"status": "received"}

    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "ip": client_ip,
        "name": payload.name,
        "email": payload.email,
        "company": payload.company,
        "country": payload.country,
        "timezone": payload.timezone,
        "team_size": payload.teamSize,
        "erp_systems": payload.erpCount,
        "entities": payload.entityCount,
        "preferred_date": payload.preferredDate,
        "preferred_time": payload.preferredTime,
        "message": payload.message,
    }

    with DATA_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")

    return {"status": "received"}


@app.get("/api/demo-requests")
async def list_demo_requests(x_admin_token: str = Header(default="")):
    check_admin_token(x_admin_token)
    return {"data": load_records()}


@app.get("/api/admin", response_class=HTMLResponse)
async def admin_view(token: str = ""):
    if token != ADMIN_TOKEN or not ADMIN_TOKEN:
        return HTMLResponse(
            "<p style='font-family:sans-serif;padding:2rem'>"
            "Missing or incorrect token. Add <code>?token=YOUR_TOKEN</code> to the URL.</p>",
            status_code=401,
        )

    records = load_records()
    columns = [
        ("received_at", "Received"), ("name", "Name"), ("email", "Email"),
        ("company", "Company"), ("country", "Country"), ("timezone", "Timezone"),
        ("team_size", "Team Size"), ("erp_systems", "ERP Systems"), ("entities", "Entities"),
        ("preferred_date", "Preferred Date"), ("preferred_time", "Preferred Time"),
        ("message", "Message"),
    ]

    def cell(record: dict, key: str) -> str:
        value = record.get(key) or "—"
        return html.escape(str(value))

    rows_html = "".join(
        "<tr>" + "".join(f"<td>{cell(r, key)}</td>" for key, _ in columns) + "</tr>"
        for r in records
    )
    header_html = "".join(f"<th>{label}</th>" for _, label in columns)

    page = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Demo Requests — Aksiom</title>
      <style>
        body {{ font-family: -apple-system, sans-serif; background: #0B0D11; color: #E6E8EC; padding: 2rem; }}
        h1 {{ font-size: 1.25rem; margin-bottom: 0.25rem; }}
        p.count {{ color: #8B92A0; margin-top: 0; margin-bottom: 1.5rem; font-size: 0.875rem; }}
        table {{ border-collapse: collapse; width: 100%; font-size: 0.8rem; }}
        th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #2A2F39; white-space: nowrap; }}
        th {{ color: #8B92A0; font-weight: 600; text-transform: uppercase; font-size: 0.65rem; letter-spacing: 0.05em; }}
        tr:hover td {{ background: #13161C; }}
        td {{ max-width: 220px; overflow: hidden; text-overflow: ellipsis; }}
      </style>
    </head>
    <body>
      <h1>Demo Requests</h1>
      <p class="count">{len(records)} submission{"s" if len(records) != 1 else ""}</p>
      <table>
        <thead><tr>{header_html}</tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(page)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
