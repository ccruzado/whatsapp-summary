"""Importa empresas desde la agenda: CSV de Google Contacts / Outlook o vCard (.vcf). Devuelve (teléfono, empresa)."""
import csv
import io
import re


def parse(filename: str, raw: bytes) -> list[tuple[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    if filename.lower().endswith(".vcf") or text.lstrip().startswith("BEGIN:VCARD"):
        return _vcf(text)
    return _csv(text)


def _vcf(text: str) -> list[tuple[str, str]]:
    text = re.sub(r"\r?\n[ \t]", "", text)  # líneas plegadas
    out = []
    for card in text.split("BEGIN:VCARD")[1:]:
        org = ""
        phones = []
        for line in card.splitlines():
            key, _, value = line.partition(":")
            name = key.split(";")[0].upper()
            if name == "ORG":
                org = value.split(";")[0].strip()
            elif name == "TEL":
                phones.append(value.strip())
        out += [(p, org) for p in phones if org]
    return out


def _csv(text: str) -> list[tuple[str, str]]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    low = [h.strip().lower() for h in header]
    org_cols = [i for i, h in enumerate(low) if h == "company" or ("organization" in h and "name" in h)]
    phone_cols = [i for i, h in enumerate(low) if "phone" in h and ("value" in h or h.endswith("phone") or "number" in h)]
    out = []
    for row in reader:
        org = next((row[i].strip() for i in org_cols if i < len(row) and row[i].strip()), "")
        if not org:
            continue
        for i in phone_cols:
            if i < len(row):
                out += [(p, org) for p in row[i].split(":::") if p.strip()]
    return out
