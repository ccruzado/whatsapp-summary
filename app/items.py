"""Pendientes/acciones con ciclo de vida: abierto → resuelto (con evidencia) o descartado.

Cada pendiente es un registro persistente: un pendiente de ayer sigue abierto hasta que una conversación posterior
(o tú, a mano) lo cierra. Así el resumen del día y de la semana salen del mismo estado, sin duplicados.
"""
import re
import sqlite3
from datetime import date, timedelta

import db

_STOP = {"de", "la", "el", "los", "las", "un", "una", "que", "con", "para", "por", "del", "al", "en", "y", "a", "se", "su", "sus", "le", "lo"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", db.norm(text)) if len(w) > 2 and w not in _STOP}


def similar(a: str, b: str) -> bool:
    """Misma acción dicha de otra forma: solapamiento alto de palabras significativas."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with db.app_conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def open_for_chat(jid: str) -> list[dict]:
    return _rows("SELECT * FROM items WHERE chat_jid=? AND estado='abierto' ORDER BY id", (jid,))


def for_chat_day(jid: str, day: str) -> list[dict]:
    """Pendientes del chat relevantes ese día: creados, resueltos ese día o aún abiertos (arrastrados)."""
    return _rows(
        """SELECT * FROM items WHERE chat_jid=? AND estado != 'descartado' AND creado_dia <= ?
           AND (estado='abierto' OR resuelto_dia >= ?) ORDER BY id""",
        (jid, day, day),
    )


def add(jid: str, accion: str, responsable: str, persona: str, plazo: str, contexto: str, day: str, hora: str) -> int:
    with db.app_conn() as c:
        cur = c.execute(
            "INSERT INTO items (chat_jid, accion, responsable, persona, plazo, contexto, creado_dia, hora_origen)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (jid, accion, responsable, persona, plazo, contexto, day, hora),
        )
        return cur.lastrowid


def resolve(item_id: int, day: str, evidencia: str, by: str = "llm") -> None:
    with db.app_conn() as c:
        c.execute(
            "UPDATE items SET estado='resuelto', resuelto_dia=?, evidencia=?, cerrado_por=? WHERE id=?",
            (day, evidencia, by, item_id),
        )


def set_state(item_id: int, estado: str, day: str) -> None:
    """Cambio manual desde la web: resuelto / descartado / abierto."""
    with db.app_conn() as c:
        if estado == "abierto":
            c.execute("UPDATE items SET estado='abierto', resuelto_dia=NULL, evidencia='', cerrado_por='' WHERE id=?", (item_id,))
        else:
            c.execute(
                "UPDATE items SET estado=?, resuelto_dia=?, cerrado_por='manual' WHERE id=?", (estado, day, item_id)
            )


def reset_chat_day(jid: str, day: str) -> None:
    """Deshace lo que el modelo registró ese día para ese chat (para poder regenerar sin duplicar)."""
    with db.app_conn() as c:
        c.execute("DELETE FROM items WHERE chat_jid=? AND creado_dia=? AND cerrado_por != 'manual'", (jid, day))
        c.execute(
            "UPDATE items SET estado='abierto', resuelto_dia=NULL, evidencia='', cerrado_por=''"
            " WHERE chat_jid=? AND resuelto_dia=? AND cerrado_por='llm'",
            (jid, day),
        )


def _enrich(rows: list[dict], labels: dict[str, dict], as_of: date) -> list[dict]:
    out = []
    for r in rows:
        info = labels.get(r["chat_jid"]) or {"label": db.chat_label(r["chat_jid"]), "group": r["chat_jid"].endswith("@g.us")}
        age = (as_of - date.fromisoformat(r["creado_dia"])).days
        out.append({**r, "label": info["label"], "company": info.get("company", ""), "age": age})
    return out


_URGENT = re.compile(r"\b(hoy|ma[nñ]ana|urgente|antes del|hasta el|lunes|martes|mi[eé]rcoles|jueves|viernes)\b|\d{1,2}[/-]\d{1,2}", re.I)


def board(as_of: date, res_from: date, res_to: date, labels: dict[str, dict], skip: set[str]) -> dict[str, list[dict]]:
    """Tablero: abiertos al final de `as_of` (míos / de otros) y resueltos entre `res_from` y `res_to`."""
    d = as_of.isoformat()
    open_rows = _rows(
        """SELECT * FROM items WHERE estado != 'descartado' AND creado_dia <= ?
           AND (estado='abierto' OR resuelto_dia > ?)""",
        (d, d),
    )
    done_rows = _rows(
        "SELECT * FROM items WHERE estado='resuelto' AND resuelto_dia >= ? AND resuelto_dia <= ?",
        (res_from.isoformat(), res_to.isoformat()),
    )
    open_rows = [r for r in open_rows if r["chat_jid"] not in skip]
    done_rows = [r for r in done_rows if r["chat_jid"] not in skip]

    def order(r: dict) -> tuple:  # urgentes primero, luego los más antiguos
        return (0 if _URGENT.search(f"{r['plazo']} {r['accion']}") else 1, -r["age"], db.norm(r["label"]))

    opened = sorted(_enrich(open_rows, labels, as_of), key=order)
    done = sorted(_enrich(done_rows, labels, as_of), key=lambda r: (db.norm(r["label"]), r["id"]))
    return {
        "mine": [r for r in opened if r["responsable"] == "yo"],
        "others": [r for r in opened if r["responsable"] != "yo"],
        "done_mine": [r for r in done if r["responsable"] == "yo"],
        "done_others": [r for r in done if r["responsable"] != "yo"],
    }


def created_between(a: date, b: date) -> int:
    with db.app_conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM items WHERE creado_dia >= ? AND creado_dia <= ? AND estado != 'descartado'",
            (a.isoformat(), b.isoformat()),
        ).fetchone()[0]
