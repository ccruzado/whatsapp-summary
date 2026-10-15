"""Extracción de pendientes con un modelo local de Ollama (salida JSON con esquema). Prompts en prompts.py."""
import json
import os
import re

import httpx

import prompts

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
CHUNK_CHARS = 9000
OWNER = os.getenv("OWNER_NAME", "Carlos Cruzado")

SCHEMA = {
    "type": "object",
    "properties": {
        "tema": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                # El orden importa: el modelo cita la evidencia antes de decidir el estado.
                "properties": {
                    "laboral": {"type": "boolean"},
                    "id": {"type": ["integer", "null"]},
                    "pedido_por": {"type": "string"},
                    "quien_debe": {"type": "string"},
                    "cliente_proyecto": {"type": "string"},
                    "accion": {"type": "string"},
                    "plazo": {"type": "string"},
                    "contexto": {"type": "string"},
                    "hora_origen": {"type": "string"},
                    "evidencia": {"type": "string"},
                    "estado": {"type": "string", "enum": ["abierto", "resuelto"]},
                },
                "required": ["laboral", "accion", "quien_debe", "cliente_proyecto", "contexto", "estado"],
            },
        },
        "datos_clave": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tema", "items", "datos_clave"],
}


def _fill(template: str, **kw: str) -> str:
    for k, v in kw.items():
        template = template.replace(f"@@{k}@@", str(v))
    return template


def _ask(prompt: str, fmt: dict | None = None) -> str:
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": _fill(prompts.SYSTEM, owner=OWNER)},
            {"role": "user", "content": prompt},
        ],
        "options": {"num_ctx": 8192, "temperature": 0.1},
    }
    if fmt:
        payload["format"] = fmt
    if MODEL.startswith("qwen3"):
        payload["think"] = False
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=900)
    r.raise_for_status()
    return re.sub(r"<think>.*?</think>", "", r.json()["message"]["content"], flags=re.S).strip()


def group_lines(lines: list[str]) -> list[list[str]]:
    """Agrupa líneas en bloques que quepan en el contexto del modelo."""
    groups, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > CHUNK_CHARS:
            groups.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        groups.append(cur)
    return groups


def extract(label: str, group: bool, company: str, day: str, lines: list[str], open_items: list[dict]) -> dict:
    """Pendientes de un bloque de conversación: {tema, items[], datos_clave[]}. Vacío si el modelo falla."""
    open_txt = "\n".join(
        f"id={o['id']} | {'yo' if o['responsable'] == 'yo' else 'otro: ' + (o['persona'] or '?')} | {o['accion']}"
        f" | abierto desde {o['creado_dia']}"
        + (f" | cliente/proyecto: {o['cliente']}" if o.get("cliente") else "")
        + (f" | nota del usuario: {o['nota']}" if o.get("nota") else "")
        for o in [o for o in open_items if o.get("estado", "abierto") == "abierto"][:25]
    ) or "(ninguno)"
    closed_txt = "\n".join(
        f"- {o['accion']} ({'hecho por el usuario' if o['estado'] == 'resuelto' else 'descartado: no aplica'})"
        + (f" | nota del usuario: {o['nota']}" if o.get("nota") else "")
        for o in open_items
        if o.get("cerrado_por") == "manual"
    ) or "(ninguno)"
    prompt = _fill(
        prompts.EXTRACT,
        kind="grupo" if group else "conversación con",
        label=label,
        company=f" (empresa: {company})" if company else "",
        day=day,
        open=open_txt,
        closed=closed_txt,
        conversation="\n".join(lines),  # al final: así un mensaje con «@@» no altera el prompt
    )
    for _ in range(2):
        try:
            data = json.loads(_ask(prompt, SCHEMA))
            if isinstance(data, dict):
                return data
        except (ValueError, httpx.HTTPError):
            continue
    return {"tema": "", "items": [], "datos_clave": []}


CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "grupos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ids": {"type": "array", "items": {"type": "integer"}}, "accion": {"type": "string"}},
                "required": ["ids", "accion"],
            },
        }
    },
    "required": ["grupos"],
}


def consolidate(label: str, open_items: list[dict]) -> list[dict]:
    """Grupos de pendientes abiertos que son la misma tarea: [{ids, accion}]."""
    lst = "\n".join(
        f"id={o['id']} | {'yo' if o['responsable'] == 'yo' else 'otro: ' + (o['persona'] or '?')} | {o['accion']}"
        + (f" | plazo: {o['plazo']}" if o["plazo"] else "")
        + (f" | cliente/proyecto: {o['cliente']}" if o["cliente"] else "")
        for o in open_items[:40]
    )
    try:
        data = json.loads(_ask(_fill(prompts.CONSOLIDATE, label=label, list=lst), CONSOLIDATE_SCHEMA))
        return [g for g in data.get("grupos", []) if isinstance(g, dict)]
    except (ValueError, httpx.HTTPError):
        return []


def topic_of(summary: str) -> str:
    m = re.search(r"\*\*Tema:\*\*\s*(.+)", summary)
    return m.group(1).strip() if m else ""


def week_narrative(start: str, end: str, topics: list[str]) -> str:
    body = "\n".join(topics)[:CHUNK_CHARS]
    return _ask(_fill(prompts.WEEK_NARRATIVE, start=start, end=end, body=body))
