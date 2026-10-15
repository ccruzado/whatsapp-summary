"""Fotos de perfil: se piden al puente (red interna), se guardan en caché y, si no hay, se dibujan iniciales."""
import hashlib
import os
import re
import threading
import time

import httpx

BRIDGE_API = os.getenv("BRIDGE_API", "http://bridge:8081")
DIR = os.getenv("AVATAR_DIR", "/data/avatars")
PHOTO_TTL = 7 * 86400
MISS_TTL = 86400
_sem = threading.Semaphore(4)  # no saturar a WhatsApp con la página llena de fotos


def _paths(jid: str) -> tuple[str, str]:
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", jid)
    base = os.path.join(DIR, safe)
    return base + ".jpg", base + ".none"


def _fresh(path: str, ttl: int) -> bool:
    return os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl


def photo(jid: str) -> bytes | None:
    os.makedirs(DIR, exist_ok=True)
    jpg, none = _paths(jid)
    if _fresh(jpg, PHOTO_TTL):
        return open(jpg, "rb").read()
    if _fresh(none, MISS_TTL):
        return None
    with _sem:
        try:
            r = httpx.get(f"{BRIDGE_API}/avatar", params={"jid": jid}, timeout=25)
        except httpx.HTTPError:
            return open(jpg, "rb").read() if os.path.exists(jpg) else None
    if r.status_code == 200 and r.content:
        open(jpg, "wb").write(r.content)
        return r.content
    open(none, "w").close()
    return None


def placeholder(jid: str, label: str) -> str:
    words = [w for w in re.split(r"\s+", re.sub(r"[^\w\s]", "", label)) if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    hue = int(hashlib.md5(jid.encode()).hexdigest()[:4], 16) % 360
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" fill="hsl({hue},45%,42%)"/>'
        '<text x="32" y="41" font-family="sans-serif" font-size="26" font-weight="600" '
        f'text-anchor="middle" fill="#fff">{initials}</text></svg>'
    )
