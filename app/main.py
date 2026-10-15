"""Web local: interacciones diarias, resúmenes y exclusión de chats."""
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import db
import service

templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    sched = BackgroundScheduler(timezone=db.TZ)
    sched.add_job(service.scheduled_job, "cron", hour=int(os.getenv("SUMMARY_HOUR", "6")), minute=0)
    sched.start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


def _parse(d: str) -> date:
    return date.fromisoformat(d)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse(f"/day/{service.today().isoformat()}")


@app.get("/day/{d}")
def day_view(request: Request, d: str):
    day = _parse(d)
    skip = db.excluded()
    msgs = db.day_messages(day)
    summaries = db.day_chat_summaries(d)
    chats = [
        {"jid": jid, **c, "summary": summaries.get(jid)}
        for jid, c in sorted(msgs.items(), key=lambda kv: -len(kv[1]["messages"]))
        if jid not in skip
    ]
    return templates.TemplateResponse(
        request,
        "day.html",
        {
            "d": d,
            "prev": (day - timedelta(days=1)).isoformat(),
            "next": (day + timedelta(days=1)).isoformat(),
            "week": service.week_start(day).isoformat(),
            "digest": db.get_summary("day", d),
            "chats": chats,
            "excluded_count": sum(1 for jid in msgs if jid in skip),
            "status": service.STATUS,
        },
    )


@app.post("/day/{d}/generate")
def day_generate(d: str, force: str = Form("")):
    service.start(service.generate_day, _parse(d), bool(force))
    return RedirectResponse(f"/day/{d}", status_code=303)


@app.get("/week")
def week_current():
    return RedirectResponse(f"/week/{service.today().isoformat()}")


@app.get("/week/{d}")
def week_view(request: Request, d: str):
    monday = service.week_start(_parse(d))
    days = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    return templates.TemplateResponse(
        request,
        "week.html",
        {
            "monday": monday.isoformat(),
            "prev": (monday - timedelta(days=7)).isoformat(),
            "next": (monday + timedelta(days=7)).isoformat(),
            "days": days,
            "digest": db.get_summary("week", monday.isoformat()),
            "status": service.STATUS,
        },
    )


@app.post("/week/{d}/generate")
def week_generate(d: str):
    service.start(service.generate_week, service.week_start(_parse(d)))
    return RedirectResponse(f"/week/{d}", status_code=303)


@app.get("/chats")
def chats_view(request: Request):
    skip = db.excluded()
    chats = [{**c, "excluded": c["jid"] in skip} for c in db.list_chats()]
    return templates.TemplateResponse(request, "chats.html", {"chats": chats, "status": service.STATUS})


@app.post("/chats/toggle")
def chats_toggle(jid: str = Form(...), excluded: str = Form("")):
    db.set_excluded(jid, bool(excluded))
    return RedirectResponse("/chats", status_code=303)
