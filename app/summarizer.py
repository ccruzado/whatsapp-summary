"""Resúmenes con un modelo local de Ollama. Los textos de los prompts viven en prompts.py."""
import os
import re

import httpx

import db
import prompts

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
CHUNK_CHARS = 9000
OWNER = os.getenv("OWNER_NAME", "Carlos Cruzado")

# Secciones tipo «**Pendientes:**»; y viñetas que solo dicen que no hay nada.
_HDR = re.compile(r"^\*\*(.+?):?\*\*:?\s*(.*)$")
_EMPTY = re.compile(
    r"^(sin (contenido|datos|pendientes|acciones|plazo|mencion\w*|informaci\w+|novedades).*|ning\w+|no hay.*|n/?a|—|-)\.?$",
    re.I,
)


def _ask(prompt: str) -> str:
    r = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "stream": False,
            "messages": [{"role": "system", "content": prompts.SYSTEM.replace("{owner}", OWNER)}, {"role": "user", "content": prompt}],
            "options": {"num_ctx": 8192, "temperature": 0.2},
            **({"think": False} if MODEL.startswith("qwen3") else {}),
        },
        timeout=900,
    )
    r.raise_for_status()
    text = r.json()["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def clean(text: str) -> str:
    """Quita secciones vacías («Sin datos…», «Ninguno») de un resumen de chat con formato **Sección:**."""
    pre, sections = [], []
    for line in text.splitlines():
        m = _HDR.match(line.strip())
        if m:
            sections.append([m.group(1).strip(), m.group(2).strip(), []])
        elif sections:
            sections[-1][2].append(line)
        else:
            pre.append(line)
    if not sections:
        return text
    out = [line for line in pre if line.strip()]
    for name, inline, body in sections:
        items = [b.strip().lstrip("-*• ").strip() for b in body if b.strip()]
        items = [i for i in items if not _EMPTY.match(i)]
        if _EMPTY.match(inline):
            inline = ""
        if not items and not inline:
            continue
        out.append(f"**{name}:**" + (f" {inline}" if inline else ""))
        out += [f"- {i}" for i in items]
    return "\n".join(out) or prompts.NO_WORK


def is_empty(text: str) -> bool:
    return text.strip().lower().startswith(prompts.NO_WORK.lower())


def _group(items: list[str]) -> list[list[str]]:
    """Agrupa textos en bloques que quepan en el contexto del modelo."""
    groups, cur, size = [], [], 0
    for it in items:
        if cur and size + len(it) > CHUNK_CHARS:
            groups.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it) + 2
    if cur:
        groups.append(cur)
    return groups


def _reduce(parts: list[str], merge) -> str:
    """Une resúmenes parciales de forma jerárquica hasta quedar uno."""
    while len(parts) > 1:
        parts = [merge("\n\n".join(g)) if len(g) > 1 else g[0] for g in _group(parts)]
    return parts[0]


def _ctx(group: bool, label: str, company: str) -> dict:
    return {
        "kind": "grupo" if group else "conversación con",
        "label": label,
        "company": f" (empresa: {company})" if company else "",
    }


def summarize_chat(label: str, group: bool, messages: list[dict], company: str = "") -> str:
    ctx = _ctx(group, label, company)
    lines = [f"{m['time']} {m['who']}: {m['text']}" for m in messages]
    parts = []
    for block in _group(lines):
        out = _ask(prompts.CHAT.format(**ctx, conversation="\n".join(block)))
        out = prompts.NO_WORK if is_empty(out) else clean(out)
        if not is_empty(out):
            parts.append(out)
    if not parts:
        return prompts.NO_WORK
    return clean(_reduce(parts, lambda body: _ask(prompts.CHAT_MERGE.format(**ctx, parts=body))))


def summarize_week_chat(label: str, group: bool, days: list[tuple[str, str]], company: str = "") -> str:
    body = "\n\n".join(f"## {d}\n{text}" for d, text in days)
    return _ask(prompts.WEEK_CHAT.format(**_ctx(group, label, company), body=body))


def summarize_week_narrative(start: str, end: str, topics: list[str]) -> str:
    body = "\n".join(topics)[:CHUNK_CHARS]
    return _ask(prompts.WEEK_NARRATIVE.format(start=start, end=end, body=body))


# ---------- armado determinista de pendientes (día y semana) ----------

_SECTION = {"pendientes": "pend", "esperando respuesta": "wait", "hecho hoy": "done", "tema": "topic", "datos clave": "data"}
_ME = re.compile(r"^\[?\s*yo\b\]?\s*[:—–-]?\s*(.*)$", re.I)
_UNASSIGNED = re.compile(r"^\[?\s*sin asignar\s*\]?\s*[:—–-]?\s*(.*)$", re.I)
_WHO = re.compile(r"^\[?([^\]\[:—–]{1,40}?)\]?\s*(?::|—|–)\s+(.+)$")
_URGENT = re.compile(
    r"\b(hoy|ma[nñ]ana|urgente|antes del|hasta el|lunes|martes|mi[eé]rcoles|jueves|viernes)\b|\d{1,2}[/-]\d{1,2}", re.I
)
_ICON = re.compile(r"✅|⏳")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_sections(text: str) -> dict[str, list[str]]:
    """Separa un resumen de chat en {pend, wait, done, topic, data}: listas de viñetas."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        m = _HDR.match(line.strip())
        if m:
            cur = _SECTION.get(m.group(1).strip().lower())
            if cur:
                out.setdefault(cur, [])
                if m.group(2).strip():
                    out[cur].append(m.group(2).strip())
        elif cur and line.strip():
            out[cur].append(line.strip().lstrip("-*• ").strip())
    return out


def split_who(item: str) -> tuple[str | None, str]:
    """(responsable, acción). 'Yo' si es del dueño; None si no hay responsable claro."""
    m = _ME.match(item)
    if m and m.group(1):
        return "Yo", m.group(1)
    m = _UNASSIGNED.match(item)
    if m:
        return None, m.group(1) or item
    m = _WHO.match(item)
    if m and len(m.group(1).split()) <= 4:
        who, what = m.group(1).strip(), m.group(2).strip()
        if db.norm(who).startswith(db.norm(OWNER.split()[0])):
            who = "Yo"
        return who, what
    return None, item


def _by(who: str | None, label: str) -> str:
    """«Persona: » salvo que sea el propio chat (1:1), donde sería redundante."""
    if not who or db.norm(who).split()[:1] == db.norm(label).split()[:1]:
        return ""
    return f"{who}: "


def _urgent_first(entries: list[tuple[str, str]]) -> list[str]:
    return [e for _, e in sorted(entries, key=lambda t: 0 if _URGENT.search(t[0]) else 1)]


def build_day_digest(items: list[tuple[str, str]]) -> str:
    """Pendientes del día agrupados en: mis pendientes / esperando a otros / completado."""
    mine, wait, done = [], [], []
    for label, text in items:
        sec = parse_sections(text)
        for it in sec.get("pend", []):
            who, what = split_who(it)
            if who == "Yo":
                mine.append((what, f"- **{label}** — {what}"))
            elif who is None:
                mine.append((what, f"- **{label}** — {what} _(sin responsable claro)_"))
            else:
                wait.append((what, f"- **{label}** — {_by(who, label)}{what}"))
        for it in sec.get("wait", []):
            who, what = split_who(it)
            if who == "Yo":
                mine.append((what, f"- **{label}** — {what}"))
            else:
                wait.append((what, f"- **{label}** — {_by(who, label)}{what}"))
        done += [(d, f"- **{label}** — {d}") for d in sec.get("done", [])]
    out = []
    for title, rows in (("Mis pendientes", mine), ("Esperando respuesta de otros", wait), ("Completado hoy", done)):
        if rows:
            out += [f"## {title} ({len(rows)})", *_urgent_first(rows), ""]
    return "\n".join(out).strip() or "Sin pendientes ni acciones laborales este día."


def week_topics(label: str, days: list[tuple[str, str]]) -> str:
    topics: list[str] = []
    for _, text in days:
        topics += [t for t in parse_sections(text).get("topic", []) if t not in topics]
    return f"{label}: " + "; ".join(topics[:3])


def build_week_report(narrative: str, status_items: list[tuple[str, str]], stale_before: str) -> str:
    """Resumen semanal: narrativa + pendientes abiertos / completados según el estado por chat."""
    open_: list[tuple[int, str]] = []
    done: list[str] = []
    stale: list[str] = []
    for label, text in status_items:
        found = False
        for line in text.splitlines():
            m = _ICON.search(line)
            if not m:
                continue
            found = True
            icon = m.group(0)
            rest = " ".join(_ICON.sub("", line).split()).lstrip("-*• ").strip()
            if not rest:
                continue
            entry = f"- **{label}** — {rest}"
            if icon == "✅":
                done.append(entry)
                continue
            open_.append((0 if re.match(r"^\[?\s*yo\b", rest, re.I) else 1, entry))
            d = _DATE.search(rest)
            if d and d.group(0) <= stale_before:
                stale.append(entry)
        if not found:
            open_.append((1, f"- **{label}** — {' '.join(text.split())} _(revisar estado)_"))
    ordered = [e for _, e in sorted(open_, key=lambda t: t[0])]
    out = ["## Resumen de la semana", narrative.strip(), ""]
    if ordered:
        out += [f"## ⏳ Pendientes abiertos ({len(ordered)})", *ordered, ""]
    if stale:
        out += [f"## ⚠️ Atención: abiertos desde inicio de semana ({len(stale)})", *stale, ""]
    if done:
        out += [f"## ✅ Completado en la semana ({len(done)})", *done, ""]
    return "\n".join(out).strip()
