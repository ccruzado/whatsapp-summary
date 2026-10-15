"""Orquestación: genera resúmenes por chat (diarios y semanales) en segundo plano."""
import threading
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


def _summarize_day_chat(period: str, jid: str, chat: dict, force: bool) -> None:
    """Resume un chat del día; se reutiliza el resumen si no cambió el número de mensajes."""
    n = len(chat["messages"])
    row = db.get_summary_row("day", period, jid)
    if not force and row and row["msg_count"] == n:
        return
    db.save_summary("day", period, jid, summarizer.summarize_chat(chat["label"], chat["group"], chat["messages"]), n)


def generate_day(day: date, force: bool = False) -> None:
    period = day.isoformat()
    skip = db.excluded()
    chats = {jid: c for jid, c in db.day_messages(day).items() if jid not in skip}
    for i, (jid, chat) in enumerate(sorted(chats.items(), key=lambda kv: db.sort_key(kv[1]["label"])), 1):
        STATUS["message"] = f"Resumiendo {chat['label']} ({i}/{len(chats)})…"
        _summarize_day_chat(period, jid, chat, force)


def generate_day_chat(day: date, jid: str) -> None:
    chat = db.day_messages(day).get(jid)
    if chat:
        STATUS["message"] = f"Resumiendo {chat['label']}…"
        _summarize_day_chat(day.isoformat(), jid, chat, True)


def generate_week(monday: date, force: bool = False) -> None:
    """Resumen semanal por chat, a partir de sus resúmenes diarios."""
    period = monday.isoformat()
    skip = db.excluded()
    per_chat = {j: v for j, v in db.day_summaries_between(monday, 7).items() if j not in skip}
    labels = {c["jid"]: c for c in db.list_chats()}
    for i, (jid, days) in enumerate(sorted(per_chat.items(), key=lambda kv: db.sort_key(labels.get(kv[0], {}).get("label", kv[0]))), 1):
        info = labels.get(jid, {"label": db.chat_label(jid), "group": jid.endswith("@g.us")})
        STATUS["message"] = f"Semana: {info['label']} ({i}/{len(per_chat)})…"
        row = db.get_summary_row("week", period, jid)
        if not force and row and row["msg_count"] == len(days):
            continue
        text = days[0][1] if len(days) == 1 else summarizer.summarize_chat_week(info["label"], info["group"], days)
        db.save_summary("week", period, jid, text, len(days))


def scheduled_job() -> None:
    """Hora programada: resume el día de hoy; los domingos, además, la semana."""
    def job():
        t = today()
        generate_day(t)
        if t.weekday() == 6:
            generate_week(week_start(t))

    start(job)
