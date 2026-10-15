"""Orquestación: resúmenes por chat, resumen del día (pendientes) y resumen semanal general."""
import threading
import zlib
from datetime import date, datetime, timedelta

import db
import summarizer

STATUS = {"running": False, "message": ""}
_lock = threading.Lock()


def today() -> date:
    return datetime.now(db.TZ).date()


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def start(target, *args) -> bool:
    """Lanza `target` en un hilo si no hay otra generación en curso."""
    if not _lock.acquire(blocking=False):
        return False

    def run():
        STATUS.update(running=True, message="Iniciando…")
        try:
            target(*args)
            STATUS["message"] = "Listo."
        except Exception as e:  # el error se muestra en la web
            STATUS["message"] = f"Error: {e}"
        finally:
            STATUS["running"] = False
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def _crc(parts: list[str]) -> int:
    return zlib.crc32("\x00".join(parts).encode())


def _summarize_day_chat(period: str, jid: str, chat: dict, force: bool) -> None:
    """Resume un chat del día; se reutiliza el resumen si no cambió el número de mensajes."""
    n = len(chat["messages"])
    row = db.get_summary_row("day", period, jid)
    if not force and row and row["msg_count"] == n:
        return
    text = summarizer.summarize_chat(chat["label"], chat["group"], chat["messages"], chat.get("company", ""))
    db.save_summary("day", period, jid, text, n)


def _day_digest(day: date, force: bool) -> None:
    """Resumen del día (pendientes y acciones) con los chats que tuvieron contenido laboral."""
    period = day.isoformat()
    skip = db.excluded()
    labels = {c["jid"]: c["label"] for c in db.list_chats()}
    items = [
        (labels.get(jid) or db.chat_label(jid), text)
        for jid, text in db.chat_summaries("day", period).items()
        if jid and jid not in skip and not summarizer.is_empty(text)
    ]
    items.sort(key=lambda it: db.sort_key(it[0]))
    # Se arma en código a partir de los resúmenes por chat: es inmediato y no pierde ni mezcla pendientes.
    db.save_summary("day", period, "", summarizer.build_day_digest(items), len(items))


def generate_day(day: date, force: bool = False) -> None:
    period = day.isoformat()
    skip = db.excluded()
    chats = {jid: c for jid, c in db.day_messages(day).items() if jid not in skip}
    for i, (jid, chat) in enumerate(sorted(chats.items(), key=lambda kv: db.sort_key(kv[1]["label"])), 1):
        STATUS["message"] = f"Resumiendo {chat['label']} ({i}/{len(chats)})…"
        _summarize_day_chat(period, jid, chat, force)
    _day_digest(day, force)


def generate_day_chat(day: date, jid: str) -> None:
    chat = db.day_messages(day).get(jid)
    if chat:
        STATUS["message"] = f"Resumiendo {chat['label']}…"
        _summarize_day_chat(day.isoformat(), jid, chat, True)
        _day_digest(day, False)


def generate_week(monday: date, force: bool = False) -> None:
    """Resumen semanal general: estado de pendientes por chat (paso 1) y consolidado (paso 2)."""
    period = monday.isoformat()
    end = (monday + timedelta(days=6)).isoformat()
    skip = db.excluded()
    labels = {c["jid"]: c for c in db.list_chats()}
    per_chat = {
        j: [(d, t) for d, t in v if not summarizer.is_empty(t)]
        for j, v in db.day_summaries_between(monday, 7).items()
        if j and j not in skip
    }
    per_chat = {j: v for j, v in per_chat.items() if v}
    ordered = sorted(per_chat.items(), key=lambda kv: db.sort_key(labels.get(kv[0], {}).get("label", kv[0])))
    status_items = []
    for i, (jid, days) in enumerate(ordered, 1):
        info = labels.get(jid) or {"label": db.chat_label(jid), "group": jid.endswith("@g.us"), "company": ""}
        STATUS["message"] = f"Semana: estado de pendientes de {info['label']} ({i}/{len(ordered)})…"
        crc = _crc([f"{d}\n{t}" for d, t in days])
        row = db.get_summary_row("weekchat", period, jid)
        if not force and row and row["msg_count"] == crc:
            text = row["text"]
        else:
            text = summarizer.summarize_week_chat(info["label"], info["group"], days, info.get("company", ""))
            db.save_summary("weekchat", period, jid, text, crc)
        if not text.strip().lower().startswith("sin acciones"):
            status_items.append((info["label"], text))
    topics = [summarizer.week_topics(labels.get(j, {}).get("label") or db.chat_label(j), d) for j, d in ordered]
    crc = _crc([f"{a}\n{b}" for a, b in status_items] + topics)
    row = db.get_summary_row("week", period, "")
    if not status_items:
        db.save_summary("week", period, "", "Sin acciones laborales registradas esta semana.", crc)
    elif force or not row or row["msg_count"] != crc:
        STATUS["message"] = "Armando resumen semanal…"
        narrative = summarizer.summarize_week_narrative(period, end, topics)
        stale_before = (monday + timedelta(days=2)).isoformat()
        db.save_summary("week", period, "", summarizer.build_week_report(narrative, status_items, stale_before), crc)


def scheduled_job() -> None:
    """Hora programada: resume el día de hoy; los domingos, además, la semana."""
    def job():
        t = today()
        generate_day(t)
        if t.weekday() == 6:
            generate_week(week_start(t))

    start(job)
