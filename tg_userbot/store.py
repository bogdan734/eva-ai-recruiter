"""SQLite: контакти, історія, денні лічильники."""
import os, sqlite3, time, datetime

DB = os.environ.get("TG_DB_PATH", "tg_recruiter.db")

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS contacts(
        peer TEXT PRIMARY KEY, name TEXT, first_sent_at REAL, status TEXT DEFAULT 'contacted')""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, peer TEXT, role TEXT, text TEXT, ts REAL)""")
    return c

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
