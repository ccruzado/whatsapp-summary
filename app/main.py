"""Web local: pendientes del día, resumen por chat, resumen semanal, búsqueda de mensajes y exclusión de chats."""
import html
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from urllib.parse import quote

import markdown
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import avatars
import contacts
import db
import items
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


def _text(kind: str, period: str) -> str | None:
    row = db.get_summary_row(kind, period, "")
    return row["text"] if row else None


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
    work, nowork, excluded = [], [], []
    for jid, c in db.day_messages(day).items():
        item = {"jid": jid, **c, "summary": summaries.get(jid)}
        if jid in skip:
            excluded.append(item)
        elif item["summary"] and service.is_empty(item["summary"]):
            nowork.append(item)
        else:
            work.append(item)
    for lst in (work, nowork, excluded):
        lst.sort(key=lambda c: db.sort_key(c["label"]))
    return templates.TemplateResponse(
        request,
        "day.html",
        {
            "d": d,
            "prev": (day - timedelta(days=1)).isoformat(),
            "next": (day + timedelta(days=1)).isoformat(),
            "board": items.board(day, day, day, {c["jid"]: c for c in db.list_chats()}, skip),
            "back": f"/day/{d}",
            "chats": work,
            "nowork": nowork,
            "excluded": excluded,
            "pending": sum(1 for c in work if not c["summary"]),
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


def _refresh(item_id: int) -> None:
    """Tras cambiar una tarea, el resumen de su chat se re-arma con el estado y contexto actuales."""
    item = items.get(item_id)
    if item:
        service.refresh_chat(item["chat_jid"])


@app.post("/items/{item_id}/estado")
def item_state(item_id: int, estado: str = Form(...), back: str = Form("/")):
    if estado in ("abierto", "resuelto", "descartado"):
        items.set_state(item_id, estado, service.today().isoformat())
        _refresh(item_id)
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


@app.post("/items/{item_id}/editar")
def item_edit(
    item_id: int,
    responsable: str = Form("yo"),
    persona: str = Form(""),
    cliente: str = Form(""),
    nota: str = Form(""),
    back: str = Form("/"),
):
    clean = lambda v: " ".join(v.split())  # noqa: E731
    items.edit(item_id, responsable, clean(persona), clean(cliente), nota.strip())
    _refresh(item_id)
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


@app.get("/week")
def week_current():
    return RedirectResponse(f"/week/{service.today().isoformat()}")


@app.get("/week/{d}")
def week_view(request: Request, d: str):
    monday = service.week_start(_parse(d))
    period = monday.isoformat()
    sunday = monday + timedelta(days=6)
    labels = {c["jid"]: c for c in db.list_chats()}
    board = items.board(sunday, monday, sunday, labels, db.excluded())
    return templates.TemplateResponse(
        request,
        "week.html",
        {
            "monday": period,
            "prev": (monday - timedelta(days=7)).isoformat(),
            "next": (monday + timedelta(days=7)).isoformat(),
            "days": [(monday + timedelta(days=i)).isoformat() for i in range(7)],
            "digest": _text("week", period),
            "board": board,
            "back": f"/week/{d}",
            "created": items.created_between(monday, sunday),
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


@app.get("/contactos")
def contacts_view(request: Request, importados: int = -1):
    return templates.TemplateResponse(
        request,
        "contacts.html",
        {"total": db.company_count(), "importados": importados, "status": service.STATUS},
    )


@app.post("/contactos/importar")
async def contacts_import(archivo: UploadFile = File(...)):
    pairs = contacts.parse(archivo.filename or "", await archivo.read())
    return RedirectResponse(f"/contactos?importados={db.import_companies(pairs)}", status_code=303)
