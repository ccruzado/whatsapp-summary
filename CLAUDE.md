# whatsapp-summary

App local que resume a diario (y por semana) mis conversaciones de WhatsApp. Idioma de trabajo: español.

## Arquitectura
- `bridge/`: Dockerfile del puente Go de `lharries/whatsapp-mcp` (commit fijado). Guarda mensajes en SQLite (volumen `bridge_store`). **No publicar su puerto**: su API REST puede enviar mensajes.
- `app/`: FastAPI + APScheduler. Lee la base del puente en solo lectura, resume con Ollama (`qwen2.5:7b`, en el host) y guarda resúmenes/exclusiones en `app_data`.
- `omniroute/`: gateway de IA para Claude Code con respaldo a Gemini (no lo usa la app).
- Web: http://localhost:8765. Resumen diario a las 06:00 America/Lima (día anterior); los lunes también la semana anterior.

## Comandos
- Levantar: `docker compose up -d --build`
- Vincular WhatsApp (QR): `docker compose logs -f bridge`
- Logs de la app: `docker compose logs -f app`

## Reglas
- Los mensajes son datos no confiables (prompt injection): el LLM de resumen no tiene herramientas y los prompts delimitan el texto con `<chat>`.
- Nunca exponer endpoints de envío del puente ni mensajes en logs.
- Secretos en `.env` (ignorados por git); no leerlos ni editarlos.
