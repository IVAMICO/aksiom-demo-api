import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "demo_requests.jsonl"

# Set this in the environment before deploying — the GET endpoint below refuses
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
    teamSize: str = ""
    erpCount: str = ""
    entityCount: str = ""
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
        "team_size": payload.teamSize,
        "erp_systems": payload.erpCount,
        "entities": payload.entityCount,
        "message": payload.message,
    }

    with DATA_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")

    return {"status": "received"}


@app.get("/api/demo-requests")
async def list_demo_requests(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not DATA_FILE.exists():
        return {"data": []}

    records = [json.loads(line) for line in DATA_FILE.read_text().splitlines() if line.strip()]
    records.reverse()
    return {"data": records}


@app.get("/api/health")
async def health():
    return {"status": "ok"}
