"""Acceso a datos: lectura (solo lectura) de la base del puente y base propia de la app."""
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BRIDGE_DB = os.getenv("BRIDGE_DB", "/bridge/messages.db")
APP_DB = os.getenv("APP_DB", "/data/app.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Lima"))


def app_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(APP_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    os.makedirs(os.path.dirname(APP_DB), exist_ok=True)
    with app_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS excluded_chats (jid TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS summaries (
                kind TEXT NOT NULL,           -- 'day' | 'week'
                period TEXT NOT NULL,         -- YYYY-MM-DD (día, o lunes de la semana)
                chat_jid TEXT NOT NULL,       -- '' = resumen global del periodo
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (kind, period, chat_jid)
            );
            """
        )


def _bridge() -> sqlite3.Connection | None:
    if not os.path.exists(BRIDGE_DB):
        return None
    conn = sqlite3.connect(f"file:{BRIDGE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def label(jid: str, name: str | None) -> str:
    return name or jid.split("@")[0]


def list_chats() -> list[dict]:
    conn = _bridge()
    if not conn:
        return []
    with conn:
        rows = conn.execute(
            "SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC"
        ).fetchall()
    return [
        {"jid": r["jid"], "label": label(r["jid"], r["name"]), "group": r["jid"].endswith("@g.us")}
        for r in rows
    ]


def excluded() -> set[str]:
    with app_conn() as c:
        return {r["jid"] for r in c.execute("SELECT jid FROM excluded_chats")}


def set_excluded(jid: str, value: bool) -> None:
    with app_conn() as c:
        if value:
            c.execute("INSERT OR IGNORE INTO excluded_chats VALUES (?)", (jid,))
        else:
            c.execute("DELETE FROM excluded_chats WHERE jid = ?", (jid,))


def _utc_bounds(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def day_messages(day: date) -> dict[str, dict]:
    """Mensajes del día (hora local) agrupados por chat: {jid: {label, group, messages[]}}."""
    conn = _bridge()
    if not conn:
        return {}
    lo, hi = _utc_bounds(day)
    with conn:
        names = {
            r["jid"].split("@")[0]: r["name"]
            for r in conn.execute("SELECT jid, name FROM chats WHERE jid LIKE '%@s.whatsapp.net'")
            if r["name"]
        }
        rows = conn.execute(
            """SELECT m.chat_jid, c.name AS chat_name, m.sender, m.content, m.timestamp,
                      m.is_from_me, m.media_type
               FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
               WHERE substr(m.timestamp, 1, 19) >= ? AND substr(m.timestamp, 1, 19) < ?
               ORDER BY m.chat_jid, m.timestamp""",
            (lo, hi),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        text = (r["content"] or "").strip() or (f"[{r['media_type']}]" if r["media_type"] else "")
        if not text:
            continue
        ts = datetime.strptime(r["timestamp"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        who = "Yo" if r["is_from_me"] else names.get(r["sender"], r["sender"])
        chat = out.setdefault(
            r["chat_jid"],
            {"label": label(r["chat_jid"], r["chat_name"]), "group": r["chat_jid"].endswith("@g.us"), "messages": []},
        )
        chat["messages"].append({"time": ts.astimezone(TZ).strftime("%H:%M"), "who": who, "text": text})
    return out


def get_summary(kind: str, period: str, chat_jid: str = "") -> str | None:
    with app_conn() as c:
        r = c.execute(
            "SELECT text FROM summaries WHERE kind=? AND period=? AND chat_jid=?", (kind, period, chat_jid)
        ).fetchone()
    return r["text"] if r else None


def save_summary(kind: str, period: str, chat_jid: str, text: str) -> None:
    with app_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO summaries VALUES (?,?,?,?,?)",
            (kind, period, chat_jid, text, datetime.now(TZ).isoformat(timespec="seconds")),
        )


def day_chat_summaries(period: str) -> dict[str, str]:
    with app_conn() as c:
        return {
            r["chat_jid"]: r["text"]
            for r in c.execute(
                "SELECT chat_jid, text FROM summaries WHERE kind='day' AND period=? AND chat_jid != ''",
                (period,),
            )
        }
