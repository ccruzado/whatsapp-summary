"""Web local: resumen por chat, interacciones diarias, búsqueda de mensajes y exclusión de chats."""
import html
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from urllib.parse import quote

import markdown
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import avatars
import db
import service

templates = Jinja2Templates(directory="templates")
templates.env.filters["md"] = lambda t: Markup(markdown.markdown(html.escape(t or ""), extensions=["nl2br"]))
templates.env.globals["avatar"] = lambda jid, label="": f"/avatar/{quote(jid)}?n={quote(label)}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    sched = BackgroundScheduler(timezone=db.TZ)
    sched.add_job(service.scheduled_job, "cron", hour=int(os.getenv("SUMMARY_HOUR", "20")), minute=0)
    sched.start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


def _parse(d: str) -> date:
    return date.fromisoformat(d)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/avatar/{jid}")
def avatar(jid: str, n: str = ""):
    data = avatars.photo(jid)
    if data:
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    return Response(avatars.placeholder(jid, n or jid), media_type="image/svg+xml", headers={"Cache-Control": "max-age=3600"})


@app.get("/")
def root():
    return RedirectResponse(f"/day/{service.today().isoformat()}")


@app.get("/day/{d}")
def day_view(request: Request, d: str):
    day = _parse(d)
    skip = db.excluded()
    summaries = db.chat_summaries("day", d)
    chats, excluded = [], []
    for jid, c in db.day_messages(day).items():
        item = {"jid": jid, **c, "summary": summaries.get(jid)}
        (excluded if jid in skip else chats).append(item)
    chats.sort(key=lambda c: db.sort_key(c["label"]))
    excluded.sort(key=lambda c: db.sort_key(c["label"]))
    return templates.TemplateResponse(
        request,
        "day.html",
        {
            "d": d,
            "prev": (day - timedelta(days=1)).isoformat(),
            "next": (day + timedelta(days=1)).isoformat(),
            "chats": chats,
            "excluded": excluded,
            "pending": sum(1 for c in chats if not c["summary"]),
            "status": service.STATUS,
        },
    )


@app.post("/day/{d}/generate")
def day_generate(d: str, force: str = Form("")):
    service.start(service.generate_day, _parse(d), bool(force))
    return RedirectResponse(f"/day/{d}", status_code=303)


@app.post("/day/{d}/chat")
def day_chat_generate(d: str, jid: str = Form(...)):
    service.start(service.generate_day_chat, _parse(d), jid)
    return RedirectResponse(f"/day/{d}", status_code=303)


@app.get("/week")
def week_current():
    return RedirectResponse(f"/week/{service.today().isoformat()}")


@app.get("/week/{d}")
def week_view(request: Request, d: str):
    monday = service.week_start(_parse(d))
    skip = db.excluded()
    summaries = db.chat_summaries("week", monday.isoformat())
    labels = {c["jid"]: c for c in db.list_chats()}
    chats = [
        {"jid": jid, "label": labels.get(jid, {}).get("label") or db.chat_label(jid), "group": jid.endswith("@g.us"), "summary": text}
        for jid, text in summaries.items()
        if jid not in skip
    ]
    chats.sort(key=lambda c: db.sort_key(c["label"]))
    return templates.TemplateResponse(
        request,
        "week.html",
        {
            "monday": monday.isoformat(),
            "prev": (monday - timedelta(days=7)).isoformat(),
            "next": (monday + timedelta(days=7)).isoformat(),
            "days": [(monday + timedelta(days=i)).isoformat() for i in range(7)],
            "chats": chats,
            "status": service.STATUS,
        },
    )


@app.post("/week/{d}/generate")
def week_generate(d: str, force: str = Form("")):
    service.start(service.generate_week, service.week_start(_parse(d)), bool(force))
    return RedirectResponse(f"/week/{d}", status_code=303)


@app.get("/messages")
def messages_view(request: Request, person: str = "", text: str = "", desde: str = "", hasta: str = ""):
    end = _parse(hasta) if hasta else service.today()
    start = _parse(desde) if desde else end - timedelta(days=7)
    searched = bool(person or text)
    results, more = db.search_messages(start, end, text, person) if searched else ([], False)
    groups: dict[str, list] = {}
    for m in results:
        groups.setdefault(m["date"], []).append(m)
    return templates.TemplateResponse(
        request,
        "messages.html",
        {
            "person": person,
            "text": text,
            "desde": start.isoformat(),
            "hasta": end.isoformat(),
            "searched": searched,
            "groups": groups,
            "total": len(results),
            "more": more,
            "people": [c["label"] for c in db.list_chats()],
            "status": service.STATUS,
        },
    )


@app.get("/chats")
def chats_view(request: Request):
    skip = db.excluded()
    chats = [{**c, "excluded": c["jid"] in skip} for c in db.list_chats()]
    return templates.TemplateResponse(
        request,
        "chats.html",
        {"chats": chats, "n_excluded": sum(1 for c in chats if c["excluded"]), "status": service.STATUS},
    )


@app.post("/chats/toggle")
def chats_toggle(jid: str = Form(...), excluded: str = Form(""), back: str = Form("/chats")):
    db.set_excluded(jid, bool(excluded))
    return RedirectResponse(back if back.startswith("/") else "/chats", status_code=303)
