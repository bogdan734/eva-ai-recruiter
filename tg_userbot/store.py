"""SQLite: контакти, історія, денні лічильники."""
import os, sqlite3, time, datetime

DB = os.environ.get("TG_DB_PATH", "tg_recruiter.db")

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS contacts(
        peer TEXT PRIMARY KEY, name TEXT, first_sent_at REAL, status TEXT DEFAULT 'contacted')""")
    c.execute("""CREATE TABLE IF NOT EXISTS outcomes(
        peer TEXT PRIMARY KEY, verdict TEXT, ts TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, peer TEXT, role TEXT, text TEXT, ts REAL)""")
    # peer -> real phone we outreached to. Lets a reply from a hidden-number account
    # dedupe onto the existing candidate/CRM card instead of minting a synthetic one.
    c.execute("""CREATE TABLE IF NOT EXISTS peer_phone(
        peer TEXT PRIMARY KEY, phone TEXT, name TEXT, ts REAL)""")
    # Why each outreach attempt succeeded or failed. Container logs die on rebuild,
    # so the only way to size problems like PRIVACY_PREMIUM_REQUIRED is to persist it.
    c.execute("""CREATE TABLE IF NOT EXISTS outreach_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, kind TEXT,
        ok INTEGER, reason TEXT, ts REAL)""")
    return c


def log_outreach(phone: str, kind: str, ok: bool, reason: str = "") -> None:
    c = _conn()
    c.execute("INSERT INTO outreach_log(phone,kind,ok,reason,ts) VALUES(?,?,?,?,?)",
              (phone, kind, 1 if ok else 0, reason[:200], time.time()))
    c.commit(); c.close()


def outreach_stats(days: int = 30) -> dict:
    c = _conn()
    since = time.time() - days * 86400
    rows = c.execute(
        "SELECT ok, reason, COUNT(*) FROM outreach_log WHERE ts>=? GROUP BY ok, reason",
        (since,)).fetchall()
    c.close()
    out = {"days": days, "sent": 0, "failed": 0, "by_reason": {}}
    for ok, reason, n in rows:
        if ok:
            out["sent"] += n
        else:
            out["failed"] += n
            out["by_reason"][reason or "unknown"] = out["by_reason"].get(reason or "unknown", 0) + n
    return out

def already_contacted(peer: str) -> bool:
    c = _conn()
    r = c.execute("SELECT 1 FROM contacts WHERE peer=?", (peer,)).fetchone()
    c.close()
    return bool(r)

def mark_contacted(peer: str, name: str):
    c = _conn()
    c.execute("INSERT OR IGNORE INTO contacts(peer,name,first_sent_at) VALUES(?,?,?)",
              (peer, name, time.time()))
    c.commit(); c.close()

def sent_today() -> int:
    midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM contacts WHERE first_sent_at>?", (midnight,)).fetchone()[0]
    c.close()
    return n

def log_message(peer: str, role: str, text: str):
    c = _conn()
    c.execute("INSERT INTO messages(peer,role,text,ts) VALUES(?,?,?,?)",
              (peer, role, text, time.time()))
    c.commit(); c.close()

def history(peer: str, limit: int = 30):
    c = _conn()
    rows = c.execute("SELECT role,text FROM messages WHERE peer=? ORDER BY id DESC LIMIT ?",
                     (peer, limit)).fetchall()
    c.close()
    return [{"role": r, "content": t} for r, t in reversed(rows)]


def last_outcome(peer: str) -> str | None:
    c = _conn()
    row = c.execute("SELECT verdict FROM outcomes WHERE peer=?", (peer,)).fetchone()
    return row[0] if row else None


def set_outcome(peer: str, verdict: str) -> None:
    import datetime as _dt
    c = _conn()
    c.execute(
        "INSERT INTO outcomes(peer,verdict,ts) VALUES(?,?,?) "
        "ON CONFLICT(peer) DO UPDATE SET verdict=excluded.verdict, ts=excluded.ts",
        (peer, verdict, _dt.datetime.utcnow().isoformat()),
    )
    c.commit()


def set_peer_phone(peer: str, phone: str, name: str = "") -> None:
    """Remember the real phone we used to reach this Telegram peer."""
    if not peer or not phone:
        return
    c = _conn()
    c.execute(
        "INSERT INTO peer_phone(peer,phone,name,ts) VALUES(?,?,?,?) "
        "ON CONFLICT(peer) DO UPDATE SET phone=excluded.phone, "
        "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE peer_phone.name END, "
        "ts=excluded.ts",
        (peer, phone, name or "", time.time()),
    )
    c.commit(); c.close()


def get_peer_phone(peer: str) -> str | None:
    c = _conn()
    row = c.execute("SELECT phone FROM peer_phone WHERE peer=?", (peer,)).fetchone()
    c.close()
    return row[0] if row else None
