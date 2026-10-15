"""Orquestación: extrae pendientes por chat (con estado verificable) y arma resúmenes del día y de la semana."""
import re
import threading
import zlib
from datetime import date, datetime, timedelta

import db
import items
import prompts
import summarizer

STATUS = {"running": False, "message": ""}
_lock = threading.Lock()


def today() -> date:
    return datetime.now(db.TZ).date()


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def is_empty(text: str) -> bool:
    return text.strip().lower().startswith(prompts.NO_WORK.lower())


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


def _hhmm(value: str | None) -> str:
    m = re.search(r"(\d{1,2}):(\d{2})", value or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _apply(res: dict, jid: str, day: str, chat: dict, times: set[str], known: list[dict]) -> bool:
    """Valida lo que devolvió el modelo y lo guarda. Devuelve True si creó o cerró algún pendiente.

    Reglas: se cierra solo con una hora de evidencia que exista en la conversación; un pendiente nuevo debe citar la hora
    del mensaje de origen; lo parecido a algo ya registrado se trata como la misma acción (sin duplicar).
    """
    changed = False
    for it in res.get("items") or []:
        accion = " ".join(str(it.get("accion") or "").split())
        if not accion or it.get("laboral") is False:
            continue
        ev = _hhmm(it.get("evidencia"))
        resolved = it.get("estado") == "resuelto" and ev in times
        target = next((k for k in known if k["id"] == it.get("id")), None) or next(
            (k for k in known if items.similar(accion, k["accion"])), None
        )
        if target:
            if resolved and target["estado"] == "abierto":
                items.resolve(target["id"], day, ev)
                target["estado"] = "resuelto"
                changed = True
            continue
        origin = _hhmm(it.get("hora_origen"))
        if origin not in times:
            continue  # sin mensaje de origen verificable: se descarta
        if resolved and ev < origin:
            resolved = False
        debe = " ".join(str(it.get("quien_debe") or "").split())
        if not debe:
            continue
        yo = db.norm(debe) in ("yo", "yo mismo") or db.norm(debe).startswith(db.norm(summarizer.OWNER.split()[0]))
        if yo and not _mine_ok(chat, origin):  # en grupos solo es mío si me nombran o yo lo dije
            yo, debe = False, "Por asignar"
        who = "yo" if yo else "otro"
        persona = "" if yo else debe
        if who == "otro" and not persona and not chat["group"]:
            persona = chat["label"]
        cliente = " ".join(str(it.get("cliente_proyecto") or "").split())
        if db.norm(cliente).startswith("no identificado"):
            cliente = chat["label"] if chat["group"] else ""
        new_id = items.add(
            jid, accion, who, persona, " ".join(str(it.get("plazo") or "").split()),
            " ".join(str(it.get("contexto") or "").split()), day, origin, cliente,
        )
        row = {"id": new_id, "accion": accion, "estado": "abierto", "responsable": who, "persona": persona, "creado_dia": day}
        if resolved:
            items.resolve(new_id, day, ev)
            row["estado"] = "resuelto"
        known.append(row)
        changed = True
    return changed


def _mine_ok(chat: dict, origin: str) -> bool:
    """En grupos, un pendiente es mío solo si el mensaje de origen es mío o me nombra."""
    if not chat["group"]:
        return True
    first = db.norm(summarizer.OWNER.split()[0])
    return any(m["time"] == origin and (m["who"] == "Yo" or first in db.norm(m["text"])) for m in chat["messages"])


def _consolidate(jid: str, label: str) -> None:
    """Fusiona pendientes abiertos que son la misma tarea (misma persona), conservando el más antiguo."""
    open_ = items.open_for_chat(jid)
    if len(open_) < 2:
        return
    by_id = {o["id"]: o for o in open_}
    for g in summarizer.consolidate(label, open_):
        ids = sorted({i for i in g.get("ids", []) if i in by_id})
        if len(ids) < 2 or len({by_id[i]["responsable"] for i in ids}) > 1:
            continue
        items.merge(ids[0], ids[1:], " ".join(str(g.get("accion") or "").split()))


def _render_chat(jid: str, day: str, tema: str, datos: list[str], changed: bool) -> str:
    """Resumen legible del chat para ese día, a partir de la base de pendientes."""
    if not changed and not datos:
        return prompts.NO_WORK
    rows = items.for_chat_day(jid, day)

    def line(r: dict) -> str:
        who = f"{r['persona']}: " if r["responsable"] != "yo" and r["persona"] else ""
        extra = [r["plazo"]] if r["plazo"] else []
        if r["estado"] == "abierto" and r["creado_dia"] < day:
            extra.append(f"desde {r['creado_dia']}")
        if r["estado"] == "resuelto" and r["evidencia"]:
            extra.append(f"✓ {r['evidencia']}")
        proyecto = f" [{r['cliente']}]" if r.get("cliente") else ""
        ctx = f" _({r['contexto']})_" if r["contexto"] else ""
        nota = f" 📝 {r['nota']}" if r.get("nota") else ""
        return f"- {who}{r['accion']}{proyecto}" + (f" — {', '.join(extra)}" if extra else "") + ctx + nota

    mine = [r for r in rows if r["estado"] == "abierto" and r["responsable"] == "yo"]
    others = [r for r in rows if r["estado"] == "abierto" and r["responsable"] != "yo"]
    done = [r for r in rows if r["estado"] == "resuelto" and r["resuelto_dia"] == day]
    dropped = items.discarded_on(jid, day)
    out = []
    if tema:
        out.append(f"**Tema:** {tema}")
    for title, lst in (
        ("Mis pendientes", mine),
        ("Pendientes de otros", others),
        ("Resuelto hoy", done),
        ("No aplica (descartado)", dropped),
    ):
        if lst:
            out += [f"**{title}:**", *[line(r) for r in lst]]
    if datos:
        out.append("**Datos clave:** " + "; ".join(datos[:5]))
    return "\n".join(out) if (rows or datos or dropped) else prompts.NO_WORK


def refresh_chat(jid: str) -> None:
    """Re-arma (sin LLM, al instante) los resúmenes del chat con el estado actual de sus pendientes.

    Se llama cada vez que cambias una tarea (hecha, descartada, reabierta) o su contexto, para que el resumen
    refleje lo hecho, lo descartado y el contexto añadido.
    """
    with db.app_conn() as c:
        rows = c.execute("SELECT period, text, msg_count FROM summaries WHERE kind='day' AND chat_jid=?", (jid,)).fetchall()
    for r in rows:
        if is_empty(r["text"]):  # un día sin contenido laboral no se «revive» por pendientes arrastrados
            continue
        tema = summarizer.topic_of(r["text"])
        m = re.search(r"\*\*Datos clave:\*\*\s*(.+)", r["text"])
        datos = [d.strip() for d in m.group(1).split(";") if d.strip()] if m else []
        db.save_summary("day", r["period"], jid, _render_chat(jid, r["period"], tema, datos, True), r["msg_count"])


def _summarize_day_chat(period: str, jid: str, chat: dict, force: bool) -> None:
    """Extrae los pendientes del chat en ese día; se reutiliza si no cambió el número de mensajes."""
    n = len(chat["messages"])
    row = db.get_summary_row("day", period, jid)
    if not force and row and row["msg_count"] == n:
        return
    items.reset_chat_day(jid, period)  # permite regenerar sin duplicar
    times = {m["time"] for m in chat["messages"]}
    lines = [f"{m['time']} {m['who']}: {m['text']}" for m in chat["messages"]]
    tema, datos, changed = "", [], False
    known: list[dict] = []
    for block in summarizer.group_lines(lines):
        known = [k for k in known if k["estado"] == "resuelto"] + items.open_for_chat(jid) + items.closed_by_user(jid)
        res = summarizer.extract(chat["label"], chat["group"], chat.get("company", ""), period, block, known)
        tema = tema or " ".join(str(res.get("tema") or "").split())
        datos += [d for d in (res.get("datos_clave") or []) if isinstance(d, str) and d.strip() and d not in datos]
        changed |= _apply(res, jid, period, chat, times, known)
    if changed:
        _consolidate(jid, chat["label"])
    db.save_summary("day", period, jid, _render_chat(jid, period, tema, datos, changed), n)


def generate_day(day: date, force: bool = False) -> None:
    period = day.isoformat()
    skip = db.excluded()
    chats = {jid: c for jid, c in db.day_messages(day).items() if jid not in skip}
    for i, (jid, chat) in enumerate(sorted(chats.items(), key=lambda kv: db.sort_key(kv[1]["label"])), 1):
        STATUS["message"] = f"Analizando {chat['label']} ({i}/{len(chats)})…"
        _summarize_day_chat(period, jid, chat, force)


def generate_day_chat(day: date, jid: str) -> None:
    chat = db.day_messages(day).get(jid)
    if chat:
        STATUS["message"] = f"Analizando {chat['label']}…"
        _summarize_day_chat(day.isoformat(), jid, chat, True)


def generate_week(monday: date, force: bool = False) -> None:
    """Narrativa semanal a partir de los temas de cada chat; los pendientes salen de la base de pendientes."""
    period = monday.isoformat()
    end = (monday + timedelta(days=6)).isoformat()
    skip = db.excluded()
    labels = {c["jid"]: c["label"] for c in db.list_chats()}
    topics: dict[str, list[str]] = {}
    for jid, days in db.day_summaries_between(monday, 7).items():
        if not jid or jid in skip:
            continue
        for _, text in days:
            t = summarizer.topic_of(text) if not is_empty(text) else ""
            if t and t not in topics.setdefault(jid, []):
                topics[jid].append(t)
    lines = [f"{labels.get(j) or db.chat_label(j)}: " + "; ".join(ts[:3]) for j, ts in topics.items() if ts]
    crc = zlib.crc32("\n".join(sorted(lines)).encode())
    row = db.get_summary_row("week", period, "")
    if not lines:
        db.save_summary("week", period, "", "Sin actividad laboral registrada esta semana.", crc)
    elif force or not row or row["msg_count"] != crc:
        STATUS["message"] = "Redactando resumen semanal…"
        db.save_summary("week", period, "", summarizer.week_narrative(period, end, sorted(lines)), crc)


def scheduled_job() -> None:
    """Hora programada: analiza el día de hoy; los domingos, además, redacta la semana."""
    def job():
        t = today()
        generate_day(t)
        if t.weekday() == 6:
            generate_week(week_start(t))

    start(job)
