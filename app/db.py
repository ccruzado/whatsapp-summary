"""Acceso a datos: lectura (solo lectura) de las bases del puente y base propia de la app."""
import os
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BRIDGE_DB = os.getenv("BRIDGE_DB", "/bridge/messages.db")
WA_DB = os.path.join(os.path.dirname(BRIDGE_DB), "whatsapp.db")
APP_DB = os.getenv("APP_DB", "/data/app.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Lima"))


def norm(s: str) -> str:
    """Minúsculas sin acentos, para ordenar y buscar."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().casefold()


def sort_key(label: str) -> tuple:
    """Orden alfabético ignorando emojis/símbolos iniciales; los números sin nombre van al final."""
    n = norm(label)
    return (label.startswith("+") or label.isdigit(), re.sub(r"^[^0-9a-z]+", "", n) or n)


# ---------- base propia ----------

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
            CREATE TABLE IF NOT EXISTS companies (key TEXT PRIMARY KEY, company TEXT NOT NULL);  -- key = últimos 9 dígitos
            CREATE TABLE IF NOT EXISTS items (   -- pendientes/acciones con ciclo de vida
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_jid TEXT NOT NULL,
                accion TEXT NOT NULL,
                responsable TEXT NOT NULL,       -- 'yo' | 'otro'
                persona TEXT NOT NULL DEFAULT '',
                plazo TEXT NOT NULL DEFAULT '',
                contexto TEXT NOT NULL DEFAULT '',
                estado TEXT NOT NULL DEFAULT 'abierto',   -- abierto | resuelto | descartado
                creado_dia TEXT NOT NULL,
                hora_origen TEXT NOT NULL DEFAULT '',
                resuelto_dia TEXT,
                evidencia TEXT NOT NULL DEFAULT '',       -- hora del mensaje que lo cerró
                cerrado_por TEXT NOT NULL DEFAULT ''      -- 'llm' | 'manual'
            );
            CREATE INDEX IF NOT EXISTS items_chat ON items (chat_jid, estado);
            CREATE TABLE IF NOT EXISTS summaries (
                kind TEXT NOT NULL,           -- 'day' | 'week'
                period TEXT NOT NULL,         -- YYYY-MM-DD (día, o lunes de la semana)
                chat_jid TEXT NOT NULL,       -- jid canónico del chat
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                msg_count INTEGER NOT NULL DEFAULT 0,  -- mensajes (día) o días (semana) resumidos
                PRIMARY KEY (kind, period, chat_jid)
            );
            """
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(summaries)")}
        if "msg_count" not in cols:
            c.execute("ALTER TABLE summaries ADD COLUMN msg_count INTEGER NOT NULL DEFAULT 0")


def excluded() -> set[str]:
    with app_conn() as c:
        return {r["jid"] for r in c.execute("SELECT jid FROM excluded_chats")}


def set_excluded(jid: str, value: bool) -> None:
    with app_conn() as c:
        if value:
            c.execute("INSERT OR IGNORE INTO excluded_chats VALUES (?)", (jid,))
        else:
            c.execute("DELETE FROM excluded_chats WHERE jid = ?", (jid,))


def phone_key(digits: str) -> str | None:
    d = "".join(ch for ch in digits if ch.isdigit())
    return d[-9:] if len(d) >= 9 else None


def import_companies(pairs: list[tuple[str, str]]) -> int:
    """Guarda (teléfono, empresa) de la agenda importada; devuelve cuántos se guardaron."""
    rows = {k: c.strip() for p, c in pairs if c.strip() and (k := phone_key(p))}
    with app_conn() as c:
        c.executemany("INSERT OR REPLACE INTO companies VALUES (?,?)", rows.items())
    global _companies
    _companies = None
    return len(rows)


_companies: dict[str, str] | None = None


def company_for(token: str) -> str:
    """Empresa del contacto (de la agenda importada), o ''."""
    global _companies
    if _companies is None:
        with app_conn() as c:
            _companies = {r["key"]: r["company"] for r in c.execute("SELECT key, company FROM companies")}
    key = phone_key(directory().pn(token))
    return _companies.get(key, "") if key else ""


def company_count() -> int:
    with app_conn() as c:
        return c.execute("SELECT COUNT(*) FROM companies").fetchone()[0]


def get_summary_row(kind: str, period: str, chat_jid: str) -> sqlite3.Row | None:
    with app_conn() as c:
        return c.execute(
            "SELECT text, msg_count FROM summaries WHERE kind=? AND period=? AND chat_jid=?",
            (kind, period, chat_jid),
        ).fetchone()


def save_summary(kind: str, period: str, chat_jid: str, text: str, msg_count: int = 0) -> None:
    with app_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO summaries VALUES (?,?,?,?,?,?)",
            (kind, period, chat_jid, text, datetime.now(TZ).isoformat(timespec="seconds"), msg_count),
        )


def chat_summaries(kind: str, period: str) -> dict[str, str]:
    with app_conn() as c:
        return {
            r["chat_jid"]: r["text"]
            for r in c.execute("SELECT chat_jid, text FROM summaries WHERE kind=? AND period=?", (kind, period))
        }


def day_summaries_between(start: date, days: int) -> dict[str, list[tuple[str, str]]]:
    """Resúmenes diarios por chat entre `start` y `start + days`: {jid: [(fecha, texto)]}."""
    out: dict[str, list[tuple[str, str]]] = {}
    with app_conn() as c:
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            for r in c.execute("SELECT chat_jid, text FROM summaries WHERE kind='day' AND period=?", (d,)):
                out.setdefault(r["chat_jid"], []).append((d, r["text"]))
    return out


# ---------- directorio de contactos (nombres) ----------

class _Directory:
    def __init__(self, names: dict[str, str], lid: dict[str, str]):
        self.names, self.lid = names, lid

    def pn(self, token: str) -> str:
        """Número de teléfono (o identificador) detrás de un jid/sender, resolviendo LID."""
        user = token.split("@")[0]
        return self.lid.get(user, user)

    def person(self, token: str) -> str:
        user = token.split("@")[0]
        pn = self.lid.get(user, user)
        return self.names.get(pn) or self.names.get(user) or phone(pn)

    def canon(self, jid: str) -> str:
        user, _, server = jid.partition("@")
        if server == "lid" and user in self.lid:
            return f"{self.lid[user]}@s.whatsapp.net"
        return jid


_dir: _Directory | None = None
_dir_at = 0.0


def phone(digits: str) -> str:
    return f"+{digits}" if digits.isdigit() and len(digits) >= 8 else digits


def directory() -> _Directory:
    global _dir, _dir_at
    if _dir and time.time() - _dir_at < 60:
        return _dir
    names: dict[str, str] = {}
    lid: dict[str, str] = {}
    if os.path.exists(WA_DB):
        conn = sqlite3.connect(f"file:{WA_DB}?mode=ro", uri=True)
        try:
            for l, p in conn.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                lid[l.split("@")[0]] = p.split("@")[0]
            for j, full, first, push, biz in conn.execute(
                "SELECT their_jid, full_name, first_name, push_name, business_name FROM whatsmeow_contacts"
            ):
                name = full or first or push or biz
                if name:
                    names[j.split("@")[0]] = name
        finally:
            conn.close()
    _dir, _dir_at = _Directory(names, lid), time.time()
    return _dir


def chat_label(jid: str, chat_name: str | None = None) -> str:
    d = directory()
    if jid.endswith("@g.us"):
        return chat_name or f"Grupo …{jid.split('@')[0][-4:]}"
    name = d.names.get(d.pn(jid))
    if name:
        return name
    if chat_name and not chat_name.replace("+", "").isdigit():
        return chat_name
    return phone(d.pn(jid))


# ---------- base del puente ----------

def _bridge() -> sqlite3.Connection | None:
    if not os.path.exists(BRIDGE_DB):
        return None
    conn = sqlite3.connect(f"file:{BRIDGE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_chats() -> list[dict]:
    """Todos los chats, con jid canónico (LID unido a su teléfono), en orden alfabético."""
    conn = _bridge()
    if not conn:
        return []
    with conn:
        rows = conn.execute("SELECT jid, name FROM chats WHERE jid NOT LIKE '%@broadcast'").fetchall()
    d = directory()
    chats: dict[str, dict] = {}
    for r in rows:
        jid = d.canon(r["jid"])
        cur = chats.get(jid)
        label = chat_label(jid, r["name"])
        if cur is None or (cur["label"].startswith("+") and not label.startswith("+")):
            chats[jid] = {"jid": jid, "label": label, "group": jid.endswith("@g.us"), "company": company_for(jid)}
    return sorted(chats.values(), key=lambda c: sort_key(c["label"]))


def _utc(day: date) -> str:
    start = datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%d %H:%M:%S")


def _shape(r: sqlite3.Row, d: _Directory) -> dict | None:
    text = (r["content"] or "").strip()
    if r["media_type"]:  # archivo/imagen enviado: cuenta como evidencia de entrega
        name = f": {r['filename']}" if r["filename"] else ""
        text = f"{text} [{r['media_type']}{name}]".strip()
    if not text:
        return None
    ts = datetime.strptime(r["timestamp"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TZ)
    jid = d.canon(r["chat_jid"])
    return {
        "jid": jid,
        "chat": chat_label(jid, r["chat_name"]),
        "group": jid.endswith("@g.us"),
        "who": "Yo" if r["is_from_me"] else d.person(r["sender"]),
        "ts": ts,
        "date": ts.strftime("%Y-%m-%d"),
        "time": ts.strftime("%H:%M"),
        "text": text,
    }


_SELECT = """SELECT m.chat_jid, c.name AS chat_name, m.sender, m.content, m.timestamp, m.is_from_me, m.media_type, m.filename
             FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid"""


def day_messages(day: date) -> dict[str, dict]:
    """Mensajes del día (hora local) por chat canónico: {jid: {label, group, messages[]}}."""
    conn = _bridge()
    if not conn:
        return {}
    with conn:
        rows = conn.execute(
            _SELECT + " WHERE substr(m.timestamp,1,19) >= ? AND substr(m.timestamp,1,19) < ?",
            (_utc(day), _utc(day + timedelta(days=1))),
        ).fetchall()
    d = directory()
    out: dict[str, dict] = {}
    for r in rows:
        m = _shape(r, d)
        if m:
            chat = out.setdefault(
                m["jid"], {"label": m["chat"], "group": m["group"], "company": company_for(m["jid"]), "messages": []}
            )
            chat["messages"].append(m)
    for chat in out.values():
        chat["messages"].sort(key=lambda m: m["ts"])
    return out


def search_messages(start: date, end: date, text: str = "", person: str = "", limit: int = 500) -> tuple[list[dict], bool]:
    """Mensajes entre fechas (incluidas), por texto y/o persona (chat o remitente). Más nuevos primero."""
    conn = _bridge()
    if not conn:
        return [], False
    sql = _SELECT + " WHERE substr(m.timestamp,1,19) >= ? AND substr(m.timestamp,1,19) < ?"
    args: list = [_utc(start), _utc(end + timedelta(days=1))]
    if text:
        sql += " AND m.content LIKE ?"
        args.append(f"%{text}%")
    sql += " ORDER BY m.timestamp DESC"
    with conn:
        rows = conn.execute(sql, args).fetchall()
    d = directory()
    q = norm(person)
    out, more = [], False
    for r in rows:
        m = _shape(r, d)
        if not m or (q and q not in norm(m["chat"]) and q not in norm(m["who"])):
            continue
        if len(out) >= limit:
            more = True
            break
        out.append(m)
    return out, more
