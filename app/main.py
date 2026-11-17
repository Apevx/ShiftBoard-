"""Shiftboard — a tiny self-hosted shift calendar.

FastAPI + SQLite. Serves a single-page UI, a read-only share page and an
iCalendar (.ics) feed that phones / Google / Outlook / Home Assistant can subscribe to.
"""
import hashlib
import json
import hmac
import os
import re
import secrets
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import exports, importer
from . import pay as paycalc

DB_PATH = os.environ.get("DB_PATH", "/data/shiftboard.db")
TZ_NAME = os.environ.get("TZ", "Australia/Sydney")
TZ = ZoneInfo(TZ_NAME)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
STATIC = Path(__file__).parent / "static"
COOKIE = "sb_session"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  code TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#3b82f6',
  all_day INTEGER NOT NULL DEFAULT 0,
  start_time TEXT,
  end_time TEXT,
  sort INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  pay_type TEXT NOT NULL DEFAULT 'ordinary',
  break_mins INTEGER NOT NULL DEFAULT 0,
  paid_hours REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assignments(
  day TEXT NOT NULL,
  shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
  ot INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, shift_id)
);
CREATE TABLE IF NOT EXISTS oncall(
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL DEFAULT 'On call',
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#f59e0b',
  note TEXT
);
CREATE TABLE IF NOT EXISTS extras(
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_extras_day ON extras(day);
CREATE TABLE IF NOT EXISTS notes(day TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS payslips(period_start TEXT PRIMARY KEY, gross REAL NOT NULL, note TEXT);
CREATE INDEX IF NOT EXISTS idx_oncall_range ON oncall(start, end);
"""

# name, code, colour, all_day, start, end, pay_type, unpaid break mins, paid hours (all-day)
DEFAULT_SHIFTS = [
    ("Day", "D", "#3b82f6", 0, "07:00", "15:30", "ordinary", 30, 0),
    ("Afternoon", "A", "#8b5cf6", 0, "15:00", "23:30", "ordinary", 30, 0),
    ("Night", "N", "#1e3a8a", 0, "23:00", "07:30", "ordinary", 30, 0),
    ("Leave", "L", "#16a34a", 1, None, None, "leave", 0, 7),
]


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_setting(c, key, default=None):
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(c, key, value):
    c.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        if "ot" not in {r[1] for r in c.execute("PRAGMA table_info(assignments)")}:
            c.execute("ALTER TABLE assignments ADD COLUMN ot INTEGER NOT NULL DEFAULT 0")
        cols = {r[1] for r in c.execute("PRAGMA table_info(shifts)")}
        if "pay_type" not in cols:  # migrate databases from before pay tracking
            c.execute("ALTER TABLE shifts ADD COLUMN pay_type TEXT NOT NULL DEFAULT 'ordinary'")
            c.execute("ALTER TABLE shifts ADD COLUMN break_mins INTEGER NOT NULL DEFAULT 0")
            c.execute("ALTER TABLE shifts ADD COLUMN paid_hours REAL NOT NULL DEFAULT 0")
            c.execute("UPDATE shifts SET break_mins=30 WHERE all_day=0")
            c.execute("UPDATE shifts SET pay_type='leave', paid_hours=7 WHERE all_day=1")
        if get_setting(c, "secret") is None:
            set_setting(c, "secret", secrets.token_hex(32))
        if get_setting(c, "share_token") is None:
            set_setting(c, "share_token", secrets.token_urlsafe(18))
        if get_setting(c, "title") is None:
            set_setting(c, "title", os.environ.get("APP_TITLE", "My Shifts"))
        if get_setting(c, "seeded") is None:
            for i, s in enumerate(DEFAULT_SHIFTS):
                c.execute(
                    "INSERT INTO shifts(name,code,color,all_day,start_time,end_time,pay_type,break_mins,paid_hours,sort) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (*s, i),
                )
            set_setting(c, "seeded", "1")


init_db()
with db() as _c:
    SECRET = get_setting(_c, "secret")

app = FastAPI(title="Shiftboard", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------- backups
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or Path(DB_PATH).parent / "backups")
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "30"))
BACKUP_NAME = re.compile(r"^shiftboard-\d{4}-\d{2}-\d{2}-\d{4,6}(-[a-z]+)?\.db$")
_backup_lock = threading.Lock()


def make_backup(tag: str = "") -> Path:
    """Consistent copy of the live database using SQLite's online backup API."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"shiftboard-{datetime.now(TZ):%Y-%m-%d-%H%M%S}{'-' + tag if tag else ''}.db"
    dest = BACKUP_DIR / name
    with _backup_lock:
        src = sqlite3.connect(DB_PATH)
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
    prune_backups()
    return dest


def list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted((p for p in BACKUP_DIR.iterdir() if BACKUP_NAME.match(p.name)), key=lambda p: p.name, reverse=True)


def prune_backups():
    auto = [p for p in list_backups() if not p.name.endswith("-prerestore.db")]
    for p in auto[BACKUP_KEEP:]:
        p.unlink(missing_ok=True)
    pre = [p for p in list_backups() if p.name.endswith("-prerestore.db")]
    for p in pre[5:]:
        p.unlink(missing_ok=True)


def _backup_loop():
    """Once a day (checked hourly): take a backup if none exists for today."""
    import time as _t
    while True:
        try:
            today = f"shiftboard-{datetime.now(TZ):%Y-%m-%d}"
            if not any(p.name.startswith(today) for p in list_backups()):
                make_backup()
        except Exception as e:  # never let the backup thread die
            print("backup failed:", e, flush=True)
        _t.sleep(3600)


@app.on_event("startup")
def _start_backups():
    threading.Thread(target=_backup_loop, daemon=True, name="backups").start()
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Cache-busting: asset URLs carry a hash of their contents so phones never run stale JS/CSS
ASSET_VERSION = hashlib.sha256(b"".join((STATIC / f).read_bytes() for f in ("app.js", "app.css"))).hexdigest()[:10]
INDEX_HTML = ((STATIC / "index.html").read_text()
              .replace("/static/app.js", f"/static/app.js?v={ASSET_VERSION}")
              .replace("/static/app.css", f"/static/app.css?v={ASSET_VERSION}"))


def index_response() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable"
                                         if request.query_params.get("v") else "no-cache")
    return resp


# ---------------------------------------------------------------- auth
def _session_value() -> str:
    msg = ("admin:" + ADMIN_PASSWORD).encode()
    return hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    if not ADMIN_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), _session_value().encode())


def require_auth(request: Request):
    if not is_authed(request):
        raise HTTPException(401, "Login required")


class LoginIn(BaseModel):
    password: str


@app.get("/api/me")
def me(request: Request):
    return {"auth_required": bool(ADMIN_PASSWORD), "authed": is_authed(request)}


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    if ADMIN_PASSWORD and not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password")
    response.set_cookie(COOKIE, _session_value(), max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------- helpers
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
LOCAL_DT = r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$"
COLOR = r"^#[0-9a-fA-F]{6}$"


def shift_hours(row) -> float:
    if row["all_day"] or not row["start_time"] or not row["end_time"]:
        return 0.0
    sh, sm = map(int, row["start_time"].split(":"))
    eh, em = map(int, row["end_time"].split(":"))
    mins = ((eh * 60 + em) - (sh * 60 + sm)) % 1440 or 1440
    return round(max(0, mins - (row["break_mins"] or 0)) / 60, 2)


def shift_dict(r) -> dict:
    d = dict(r)
    d["all_day"] = bool(d["all_day"])
    d["archived"] = bool(d["archived"])
    d["hours"] = shift_hours(r)
    return d


def check_range(start: date, end: date):
    if end < start or (end - start).days > 800:
        raise HTTPException(400, "Bad date range")


def load_state(c, start: date, end: date, public: bool) -> dict:
    check_range(start, end)
    shifts = [shift_dict(r) for r in c.execute("SELECT * FROM shifts ORDER BY archived, sort, id")]
    assignments = [
        dict(r)
        for r in c.execute(
            "SELECT day, shift_id, ot FROM assignments WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        )
    ]
    range_start = f"{start.isoformat()}T00:00"
    range_end = f"{(end + timedelta(days=1)).isoformat()}T00:00"
    oncall = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM oncall WHERE start < ? AND end > ? ORDER BY start", (range_end, range_start)
        )
    ]
    out = {
        "title": get_setting(c, "title"),
        "shifts": shifts,
        "assignments": assignments,
        "oncall": oncall,
    }
    if public:
        for o in out["oncall"]:
            o.pop("note", None)
    else:
        out["extras"] = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM extras WHERE day BETWEEN ? AND ? ORDER BY day, start",
                (start.isoformat(), end.isoformat()),
            )
        ]
        out["notes"] = {
            r["day"]: r["text"]
            for r in c.execute(
                "SELECT day, text FROM notes WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
            )
        }
        out["share_token"] = get_setting(c, "share_token")
        out["public_url"] = PUBLIC_URL
        out["auth_required"] = bool(ADMIN_PASSWORD)
    return out


def check_token(c, token: str):
    real = get_setting(c, "share_token") or ""
    if not hmac.compare_digest(token.encode(), real.encode()):
        raise HTTPException(404, "Not found")


# ---------------------------------------------------------------- state
@app.get("/api/state", dependencies=[Depends(require_auth)])
def state(start: date, end: date):
    with db() as c:
        return load_state(c, start, end, public=False)


@app.get("/api/public/{token}/state")
def public_state(token: str, start: date, end: date):
    with db() as c:
        check_token(c, token)
        return load_state(c, start, end, public=True)


# ---------------------------------------------------------------- shifts
class ShiftIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    code: str = Field(min_length=1, max_length=4)
    color: str = Field(pattern=COLOR)
    all_day: bool = False
    start_time: Optional[str] = Field(default=None, pattern=HHMM)
    end_time: Optional[str] = Field(default=None, pattern=HHMM)
    archived: bool = False
    pay_type: Literal["ordinary", "overtime", "leave", "unpaid"] = "ordinary"
    break_mins: int = Field(default=0, ge=0, le=240)
    paid_hours: float = Field(default=0, ge=0, le=24)


def _validate_shift(s: ShiftIn):
    if not s.all_day and (not s.start_time or not s.end_time):
        raise HTTPException(400, "Timed shifts need a start and end time")
    if s.all_day:
        s.start_time = s.end_time = None
        s.break_mins = 0
    else:
        s.paid_hours = 0


@app.post("/api/shifts", dependencies=[Depends(require_auth)])
def create_shift(s: ShiftIn):
    _validate_shift(s)
    with db() as c:
        sort = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM shifts").fetchone()[0]
        cur = c.execute(
            "INSERT INTO shifts(name,code,color,all_day,start_time,end_time,pay_type,break_mins,paid_hours,sort) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (s.name.strip(), s.code.strip(), s.color, int(s.all_day), s.start_time, s.end_time,
             s.pay_type, s.break_mins, s.paid_hours, sort),
        )
        return shift_dict(c.execute("SELECT * FROM shifts WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/shifts/{sid}", dependencies=[Depends(require_auth)])
def update_shift(sid: int, s: ShiftIn):
    _validate_shift(s)
    with db() as c:
        cur = c.execute(
            "UPDATE shifts SET name=?,code=?,color=?,all_day=?,start_time=?,end_time=?,archived=?,"
            "pay_type=?,break_mins=?,paid_hours=? WHERE id=?",
            (s.name.strip(), s.code.strip(), s.color, int(s.all_day), s.start_time, s.end_time, int(s.archived),
             s.pay_type, s.break_mins, s.paid_hours, sid),
        )
        if not cur.rowcount:
            raise HTTPException(404, "Shift not found")
        return shift_dict(c.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone())


@app.delete("/api/shifts/{sid}", dependencies=[Depends(require_auth)])
def delete_shift(sid: int):
    """Shifts already on the calendar are archived (hidden from the palette) so history survives."""
    with db() as c:
        used = c.execute("SELECT COUNT(*) FROM assignments WHERE shift_id=?", (sid,)).fetchone()[0]
        if used:
            c.execute("UPDATE shifts SET archived=1 WHERE id=?", (sid,))
            return {"archived": True, "used": used}
        c.execute("DELETE FROM shifts WHERE id=?", (sid,))
        return {"deleted": True}


class OrderIn(BaseModel):
    ids: list[int]


@app.put("/api/shifts-order", dependencies=[Depends(require_auth)])
def reorder(body: OrderIn):
    with db() as c:
        for i, sid in enumerate(body.ids):
            c.execute("UPDATE shifts SET sort=? WHERE id=?", (i, sid))
    return {"ok": True}


# ---------------------------------------------------------------- assignments
class AssignIn(BaseModel):
    day: date
    shift_id: Optional[int] = None
    action: Literal["add", "replace", "remove", "clear", "set_ot"] = "replace"
    ot: bool = False


def _day_ids(c, day: str) -> list[int]:
    return [r[0] for r in c.execute("SELECT shift_id FROM assignments WHERE day=?", (day,))]


def _day_ot(c, day: str) -> list[int]:
    return [r[0] for r in c.execute("SELECT shift_id FROM assignments WHERE day=? AND ot=1", (day,))]


@app.post("/api/assign", dependencies=[Depends(require_auth)])
def assign(a: AssignIn):
    day = a.day.isoformat()
    with db() as c:
        if a.action in ("clear", "replace"):
            c.execute("DELETE FROM assignments WHERE day=?", (day,))
        if a.action in ("add", "replace"):
            if a.shift_id is None:
                raise HTTPException(400, "shift_id required")
            c.execute(
                "INSERT INTO assignments(day, shift_id, ot) VALUES(?,?,?) "
                "ON CONFLICT(day, shift_id) DO UPDATE SET ot=excluded.ot",
                (day, a.shift_id, int(a.ot)),
            )
        if a.action == "set_ot":
            c.execute("UPDATE assignments SET ot=? WHERE day=? AND shift_id=?", (int(a.ot), day, a.shift_id))
        if a.action == "remove":
            c.execute("DELETE FROM assignments WHERE day=? AND shift_id=?", (day, a.shift_id))
        return {"day": day, "shift_ids": _day_ids(c, day), "ot_ids": _day_ot(c, day)}


class PatternIn(BaseModel):
    start: date
    end: date
    pattern: list[Optional[int]] = Field(min_length=1, max_length=366)
    replace: bool = True


@app.post("/api/pattern", dependencies=[Depends(require_auth)])
def apply_pattern(p: PatternIn):
    """Repeat a rotation (list of shift ids, null = day off) from start to end inclusive."""
    check_range(p.start, p.end)
    n = 0
    with db() as c:
        d = p.start
        while d <= p.end:
            sid = p.pattern[(d - p.start).days % len(p.pattern)]
            iso = d.isoformat()
            if p.replace:
                c.execute("DELETE FROM assignments WHERE day=?", (iso,))
            if sid is not None:
                c.execute("INSERT OR IGNORE INTO assignments(day, shift_id) VALUES(?,?)", (iso, sid))
                n += 1
            d += timedelta(days=1)
    return {"assigned": n}


class RangeIn(BaseModel):
    start: date
    end: date


@app.post("/api/clear", dependencies=[Depends(require_auth)])
def clear_range(r: RangeIn):
    check_range(r.start, r.end)
    with db() as c:
        cur = c.execute(
            "DELETE FROM assignments WHERE day BETWEEN ? AND ?", (r.start.isoformat(), r.end.isoformat())
        )
        return {"removed": cur.rowcount}


# ---------------------------------------------------------------- notes
class NoteIn(BaseModel):
    text: str = Field(max_length=2000)


@app.put("/api/notes/{day}", dependencies=[Depends(require_auth)])
def put_note(day: date, n: NoteIn):
    with db() as c:
        if n.text.strip():
            c.execute(
                "INSERT INTO notes(day,text) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET text=excluded.text",
                (day.isoformat(), n.text),
            )
        else:
            c.execute("DELETE FROM notes WHERE day=?", (day.isoformat(),))
    return {"ok": True}


# ---------------------------------------------------------------- on-call
class OnCallIn(BaseModel):
    label: str = Field(default="On call", min_length=1, max_length=40)
    start: str = Field(pattern=LOCAL_DT)
    end: str = Field(pattern=LOCAL_DT)
    color: str = Field(default="#f59e0b", pattern=COLOR)
    note: Optional[str] = Field(default=None, max_length=1000)


def _validate_oncall(o: OnCallIn):
    if o.end <= o.start:
        raise HTTPException(400, "On-call must end after it starts")


@app.post("/api/oncall", dependencies=[Depends(require_auth)])
def create_oncall(o: OnCallIn):
    _validate_oncall(o)
    with db() as c:
        cur = c.execute(
            "INSERT INTO oncall(label,start,end,color,note) VALUES(?,?,?,?,?)",
            (o.label.strip(), o.start, o.end, o.color, o.note),
        )
        return dict(c.execute("SELECT * FROM oncall WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/oncall/{oid}", dependencies=[Depends(require_auth)])
def update_oncall(oid: int, o: OnCallIn):
    _validate_oncall(o)
    with db() as c:
        cur = c.execute(
            "UPDATE oncall SET label=?,start=?,end=?,color=?,note=? WHERE id=?",
            (o.label.strip(), o.start, o.end, o.color, o.note, oid),
        )
        if not cur.rowcount:
            raise HTTPException(404, "Not found")
        return dict(c.execute("SELECT * FROM oncall WHERE id=?", (oid,)).fetchone())


@app.delete("/api/oncall/{oid}", dependencies=[Depends(require_auth)])
def delete_oncall(oid: int):
    with db() as c:
        c.execute("DELETE FROM oncall WHERE id=?", (oid,))
    return {"ok": True}


# ---------------------------------------------------------------- extra work (call-ins, recall, overtime)
class ExtraIn(BaseModel):
    day: date
    kind: Literal["callin", "remote", "overtime"] = "callin"
    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=HHMM)
    note: Optional[str] = Field(default=None, max_length=500)


@app.post("/api/extras", dependencies=[Depends(require_auth)])
def create_extra(x: ExtraIn):
    if x.start == x.end:
        raise HTTPException(400, "Start and end can't be the same")
    with db() as c:
        cur = c.execute(
            "INSERT INTO extras(day,kind,start,end,note) VALUES(?,?,?,?,?)",
            (x.day.isoformat(), x.kind, x.start, x.end, x.note),
        )
        return dict(c.execute("SELECT * FROM extras WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/extras/{xid}", dependencies=[Depends(require_auth)])
def update_extra(xid: int, x: ExtraIn):
    if x.start == x.end:
        raise HTTPException(400, "Start and end can't be the same")
    with db() as c:
        cur = c.execute(
            "UPDATE extras SET day=?,kind=?,start=?,end=?,note=? WHERE id=?",
            (x.day.isoformat(), x.kind, x.start, x.end, x.note, xid),
        )
        if not cur.rowcount:
            raise HTTPException(404, "Not found")
        return dict(c.execute("SELECT * FROM extras WHERE id=?", (xid,)).fetchone())


@app.delete("/api/extras/{xid}", dependencies=[Depends(require_auth)])
def delete_extra(xid: int):
    with db() as c:
        c.execute("DELETE FROM extras WHERE id=?", (xid,))
    return {"ok": True}


# ---------------------------------------------------------------- pay
def pay_settings(c) -> dict:
    s = dict(paycalc.DEFAULT_SETTINGS)
    raw = get_setting(c, "pay")
    if raw:
        try:
            s.update(json.loads(raw))
        except ValueError:
            pass
    return s


class PaySettingsIn(BaseModel):
    classification: str = ""
    hourly_override: Optional[float] = Field(default=None, ge=0, le=1000)
    fortnight_start: date
    extra_holidays: list[date] = []
    not_holidays: list[date] = []


@app.get("/api/pay-settings", dependencies=[Depends(require_auth)])
def get_pay_settings():
    with db() as c:
        return {"settings": pay_settings(c), "classifications": paycalc.classifications(),
                "sources": "Medical Radiation Scientists (State) Award 2025; Conditions of Employment (State) Award 2025"}


@app.put("/api/pay-settings", dependencies=[Depends(require_auth)])
def put_pay_settings(p: PaySettingsIn):
    if p.classification and p.classification not in paycalc.RADIOGRAPHER:
        raise HTTPException(400, "Unknown classification")
    data = p.model_dump(mode="json")
    with db() as c:
        set_setting(c, "pay", json.dumps(data))
    return {"ok": True}


def pay_for(c, start: date, ps: dict) -> dict:
    """Pay estimate for the fortnight starting on `start`."""
    end = start + timedelta(days=13)
    shifts = {r["id"]: dict(r) for r in c.execute("SELECT * FROM shifts")}
    assignments = [dict(r) for r in c.execute(
        "SELECT day, shift_id, ot FROM assignments WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))]
    extras = [dict(r) for r in c.execute(
        "SELECT * FROM extras WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))]
    hi = f"{(end + timedelta(days=1)).isoformat()}T00:00"
    lo = f"{(start - timedelta(days=60)).isoformat()}T00:00"
    oncall = [dict(r) for r in c.execute("SELECT * FROM oncall WHERE end > ? AND start < ?", (lo, hi))]
    out = paycalc.compute(start, shifts, assignments, extras, oncall, ps)
    row = c.execute("SELECT gross, note FROM payslips WHERE period_start=?", (start.isoformat(),)).fetchone()
    out["payslip"] = None
    if row:
        out["payslip"] = {"gross": row["gross"], "note": row["note"],
                          "difference": round(row["gross"] - out.get("gross", 0), 2)}
    return out


@app.get("/api/pay", dependencies=[Depends(require_auth)])
def get_pay(on: Optional[date] = None):
    with db() as c:
        ps = pay_settings(c)
        start = paycalc.fortnight_containing(on or datetime.now(TZ).date(), ps)
        out = pay_for(c, start, ps)
    out.pop("lines", None)
    return out


# ---------------------------------------------------------------- exports
def _dl(body: bytes, media: str, filename: str) -> Response:
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export/month.pdf", dependencies=[Depends(require_auth)])
def export_month_pdf(month: str, sun: bool = False, notes: bool = True):
    try:
        y, m = map(int, month.split("-"))
        first = date(y, m, 1)
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    with db() as c:
        st = load_state(c, first - timedelta(days=7), last + timedelta(days=7), public=False)
        extras = [dict(r) for r in c.execute(
            "SELECT * FROM extras WHERE day BETWEEN ? AND ?", (first.isoformat(), last.isoformat()))]
    shifts = {s["id"]: s for s in st["shifts"]}
    pdf = exports.month_pdf(y, m, st["title"], shifts, st["assignments"], st["oncall"], extras,
                            st["notes"] if notes else {}, week_start_sun=sun)
    return _dl(pdf, "application/pdf", f"shifts-{y}-{m:02d}.pdf")


@app.get("/api/export/hours.xlsx", dependencies=[Depends(require_auth)])
def export_hours(start: date, end: date):
    if end < start or (end - start).days > 800:
        raise HTTPException(400, "Bad date range")
    with db() as c:
        ps = pay_settings(c)
        title = get_setting(c, "title")
        f = paycalc.fortnight_containing(start, ps)
        fortnights = []
        while f <= end:
            fortnights.append(pay_for(c, f, ps))
            f += timedelta(days=14)
    body = exports.hours_xlsx(start, end, fortnights, f"{title} — hours & pay")
    return _dl(body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               f"shifts-{start.isoformat()}-to-{end.isoformat()}.xlsx")


# ---------------------------------------------------------------- roster import
@app.post("/api/import/preview", dependencies=[Depends(require_auth)])
async def import_preview(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "File is too large (15 MB max)")
    try:
        sheets = importer.read_book(data, file.filename or "roster.xlsx")
    except Exception as e:  # unreadable / wrong format
        raise HTTPException(400, f"Couldn't read that file ({type(e).__name__}). Save it as .xlsx or .csv and try again.")
    series = importer.find_series(sheets, datetime.now(TZ).date())
    if not series:
        raise HTTPException(400, "Couldn't find any dates in that file. Dates need to be in a header row or a column.")
    with db() as c:
        aliases = json.loads(get_setting(c, "import_aliases") or "{}")
    return {"series": series, "aliases": aliases, "filename": file.filename}


class PasteIn(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)
    first_date: Optional[date] = None


@app.post("/api/import/paste", dependencies=[Depends(require_auth)])
def import_paste(p: PasteIn):
    sheets = importer.read_text(p.text)
    today = datetime.now(TZ).date()
    series = importer.find_series(sheets, today)
    if not series:
        series = importer.series_from_daynums(sheets, p.first_date, today)
    if not series and p.first_date:
        series = importer.series_from_first_date(sheets, p.first_date)
    if not series:
        raise HTTPException(400, "No dates found. Copy the row of dates along with your shifts, "
                                 "or enter the date of the first day you copied.")
    with db() as c:
        aliases = json.loads(get_setting(c, "import_aliases") or "{}")
    return {"series": series, "aliases": aliases, "filename": None}


class ImportItem(BaseModel):
    day: date
    shift_id: int
    ot: bool = False


class ImportOnCall(BaseModel):
    start: str = Field(pattern=LOCAL_DT)
    end: str = Field(pattern=LOCAL_DT)
    label: str = Field(default="On call", max_length=40)


class ImportApplyIn(BaseModel):
    start: date
    end: date
    replace: bool = True
    items: list[ImportItem] = []
    oncall: list[ImportOnCall] = []
    aliases: dict[str, str] = {}


@app.post("/api/import/apply", dependencies=[Depends(require_auth)])
def import_apply(p: ImportApplyIn):
    check_range(p.start, p.end)
    with db() as c:
        valid = {r[0] for r in c.execute("SELECT id FROM shifts")}
        if p.replace:
            c.execute("DELETE FROM assignments WHERE day BETWEEN ? AND ?", (p.start.isoformat(), p.end.isoformat()))
        n = 0
        for it in p.items:
            if it.shift_id in valid and p.start <= it.day <= p.end:
                c.execute("INSERT INTO assignments(day, shift_id, ot) VALUES(?,?,?) "
                          "ON CONFLICT(day, shift_id) DO UPDATE SET ot=excluded.ot",
                          (it.day.isoformat(), it.shift_id, int(it.ot)))
                n += 1
        oc = 0
        for o in p.oncall:
            if o.end <= o.start:
                continue
            if not c.execute("SELECT 1 FROM oncall WHERE start=? AND end=?", (o.start, o.end)).fetchone():
                c.execute("INSERT INTO oncall(label,start,end) VALUES(?,?,?)", (o.label, o.start, o.end))
                oc += 1
        saved = json.loads(get_setting(c, "import_aliases") or "{}")
        saved.update({k[:40]: v[:20] for k, v in p.aliases.items()})
        set_setting(c, "import_aliases", json.dumps(saved))
    return {"assigned": n, "oncall": oc}


# ---------------------------------------------------------------- backup endpoints
def _backup_info(p: Path) -> dict:
    st = p.stat()
    return {"name": p.name, "size": st.st_size,
            "created": datetime.fromtimestamp(st.st_mtime, TZ).strftime("%Y-%m-%dT%H:%M")}


@app.get("/api/backups", dependencies=[Depends(require_auth)])
def get_backups():
    return {"dir": str(BACKUP_DIR), "keep": BACKUP_KEEP, "backups": [_backup_info(p) for p in list_backups()]}


@app.post("/api/backups", dependencies=[Depends(require_auth)])
def backup_now():
    return _backup_info(make_backup())


@app.get("/api/backups/{name}", dependencies=[Depends(require_auth)])
def download_backup(name: str):
    if not BACKUP_NAME.match(name) or not (BACKUP_DIR / name).exists():
        raise HTTPException(404, "Not found")
    return FileResponse(BACKUP_DIR / name, media_type="application/octet-stream", filename=name)


@app.get("/api/backup/download", dependencies=[Depends(require_auth)])
def download_fresh_backup():
    p = make_backup()
    return FileResponse(p, media_type="application/octet-stream", filename=p.name)


REQUIRED_TABLES = {"shifts", "assignments", "oncall", "settings"}


@app.post("/api/backup/restore", dependencies=[Depends(require_auth)])
async def restore_backup(file: UploadFile = File(...)):
    data = await file.read()
    if not data.startswith(b"SQLite format 3\x00"):
        raise HTTPException(400, "That isn't a Shiftboard backup (.db) file")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        chk = sqlite3.connect(tmp)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            chk.close()
        if ok != "ok" or not REQUIRED_TABLES <= tables:
            raise HTTPException(400, "That backup is damaged or isn't from Shiftboard")
        safety = make_backup("prerestore")
        with _backup_lock:
            src = sqlite3.connect(tmp)
            dst = sqlite3.connect(DB_PATH)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        init_db()  # bring older backups up to the current schema
    finally:
        os.unlink(tmp)
    return {"ok": True, "safety_copy": safety.name}


# ---------------------------------------------------------------- payslips
class PayslipIn(BaseModel):
    gross: float = Field(ge=0, le=100000)
    note: Optional[str] = Field(default=None, max_length=300)


@app.put("/api/payslips/{period_start}", dependencies=[Depends(require_auth)])
def put_payslip(period_start: date, p: PayslipIn):
    with db() as c:
        start = paycalc.fortnight_containing(period_start, pay_settings(c))
        c.execute("INSERT INTO payslips(period_start,gross,note) VALUES(?,?,?) "
                  "ON CONFLICT(period_start) DO UPDATE SET gross=excluded.gross, note=excluded.note",
                  (start.isoformat(), p.gross, p.note))
    return {"ok": True, "period_start": start.isoformat()}


@app.delete("/api/payslips/{period_start}", dependencies=[Depends(require_auth)])
def delete_payslip(period_start: date):
    with db() as c:
        start = paycalc.fortnight_containing(period_start, pay_settings(c))
        c.execute("DELETE FROM payslips WHERE period_start=?", (start.isoformat(),))
    return {"ok": True}


@app.get("/api/payslips", dependencies=[Depends(require_auth)])
def payslip_history(limit: int = 12):
    with db() as c:
        ps = pay_settings(c)
        rows = c.execute("SELECT period_start FROM payslips ORDER BY period_start DESC LIMIT ?",
                         (max(1, min(limit, 60)),)).fetchall()
        out = []
        for r in rows:
            f = pay_for(c, date.fromisoformat(r[0]), ps)
            out.append({"start": f["start"], "end": f["end"], "estimate": f.get("gross", 0),
                        "payslip": f["payslip"]["gross"], "difference": f["payslip"]["difference"],
                        "note": f["payslip"]["note"]})
    return {"history": out}


# ---------------------------------------------------------------- settings / sharing
class SettingsIn(BaseModel):
    title: str = Field(min_length=1, max_length=60)


@app.put("/api/settings", dependencies=[Depends(require_auth)])
def put_settings(s: SettingsIn):
    with db() as c:
        set_setting(c, "title", s.title.strip())
    return {"ok": True}


@app.post("/api/share/rotate", dependencies=[Depends(require_auth)])
def rotate_share():
    with db() as c:
        tok = secrets.token_urlsafe(18)
        set_setting(c, "share_token", tok)
    return {"share_token": tok}


# ---------------------------------------------------------------- iCalendar feed
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    out, b = [], line.encode()
    while len(b) > 75:
        cut = 75 if not out else 74
        while cut > 0 and (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 char
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return [out[0]] + [" " + x for x in out[1:]]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


@app.get("/s/{token}/calendar.ics")
def ics_feed(token: str):
    today = datetime.now(TZ).date()
    with db() as c:
        check_token(c, token)
        st = load_state(c, today - timedelta(days=120), today + timedelta(days=500), public=True)
    by_id = {s["id"]: s for s in st["shifts"]}
    stamp = _utc(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Shiftboard//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(st['title'])}",
        f"X-WR-TIMEZONE:{TZ_NAME}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for a in st["assignments"]:
        s = by_id.get(a["shift_id"])
        if not s:
            continue
        d = date.fromisoformat(a["day"])
        ev = ["BEGIN:VEVENT", f"UID:shift-{a['day']}-{s['id']}@shiftboard", f"DTSTAMP:{stamp}"]
        if s["all_day"]:
            ev += [
                f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
                "TRANSP:TRANSPARENT",
            ]
        else:
            start = datetime.combine(d, time.fromisoformat(s["start_time"]), TZ)
            end = datetime.combine(d, time.fromisoformat(s["end_time"]), TZ)
            if end <= start:
                end += timedelta(days=1)
            ev += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}", "TRANSP:OPAQUE"]
        name = s["name"] + (" (overtime)" if a.get("ot") else "")
        ev += [f"SUMMARY:{_esc(name)}", "CATEGORIES:Shift", "END:VEVENT"]
        lines += ev
    for o in st["oncall"]:
        lines += [
            "BEGIN:VEVENT",
            f"UID:oncall-{o['id']}@shiftboard",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(_local(o['start']))}",
            f"DTEND:{_utc(_local(o['end']))}",
            f"SUMMARY:{_esc(o['label'])}",
            "CATEGORIES:On call",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(x for line in lines for x in _fold(line)) + "\r\n"
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="shifts.ics"', "Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------- pages
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return index_response()


@app.get("/s/{token}")
def share_page(token: str):
    with db() as c:
        check_token(c, token)
    return index_response()
