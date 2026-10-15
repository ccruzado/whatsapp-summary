"""Orquestación: genera resúmenes diarios y semanales en segundo plano."""
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


def generate_day(day: date, force: bool = False) -> None:
    period = day.isoformat()
    skip = db.excluded()
    chats = {jid: c for jid, c in db.day_messages(day).items() if jid not in skip}
    done = []
    for i, (jid, chat) in enumerate(chats.items(), 1):
        STATUS["message"] = f"Resumiendo {chat['label']} ({i}/{len(chats)})…"
        text = None if force else db.get_summary("day", period, jid)
        if text is None:
            text = summarizer.summarize_chat(chat["label"], chat["group"], chat["messages"])
            db.save_summary("day", period, jid, text)
        done.append((chat["label"], text))
    if not done:
        db.save_summary("day", period, "", "Sin conversaciones para resumir este día.")
        return
    STATUS["message"] = "Armando resumen del día…"
    db.save_summary("day", period, "", summarizer.digest_day(period, done))


def generate_week(monday: date) -> None:
    days = []
    for i in range(7):
        d = (monday + timedelta(days=i)).isoformat()
        text = db.get_summary("day", d)
        if text and not text.startswith("Sin conversaciones"):
            days.append((d, text))
    end = (monday + timedelta(days=6)).isoformat()
    if not days:
        db.save_summary("week", monday.isoformat(), "", "No hay resúmenes diarios en esta semana.")
        return
    STATUS["message"] = "Armando resumen semanal…"
    db.save_summary("week", monday.isoformat(), "", summarizer.digest_week(monday.isoformat(), end, days))


def scheduled_job() -> None:
    """Hora programada: resume el día anterior; los lunes, además, la semana anterior."""
    yesterday = today() - timedelta(days=1)

    def job():
        generate_day(yesterday)
        if today().weekday() == 0:
            generate_week(week_start(yesterday))

    start(job)
