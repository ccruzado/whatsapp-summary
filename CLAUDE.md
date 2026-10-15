# whatsapp-summary

App local que resume a diario (y por semana) mis conversaciones de WhatsApp. Idioma de trabajo: español.

## Arquitectura
- `bridge/`: Dockerfile del puente Go de `lharries/whatsapp-mcp` (commit fijado; se actualiza whatsmeow y se parchea `main.go` con `patch-whatsmeow.sh`). `avatar.go` añade un endpoint interno `:8081/avatar` para fotos de perfil. Guarda mensajes en SQLite (volumen `bridge_store`). **No publicar sus puertos** (8080 puede enviar mensajes).
- `app/`: FastAPI + APScheduler. Lee la base del puente en solo lectura, resume con Ollama (`qwen3:8b`, en el host) y guarda resúmenes/exclusiones en `app_data`.
- Resúmenes (foco laboral: pendientes y acciones): por chat con el LLM (`app/prompts.py`, editable; `docker compose restart app` basta); el resumen del día y las secciones del semanal se arman en código (`summarizer.build_*`) para no perder pendientes. El semanal reconcilia ✅/⏳ por chat con el LLM y añade una narrativa. Modelo por defecto `qwen3:8b` (`OLLAMA_MODEL`); `OWNER_NAME` = cómo te nombran en los chats.
- Empresa del contacto: WhatsApp no la entrega; se importa desde /contactos (CSV Google/.vcf) y se cruza por los últimos 9 dígitos.
- `omniroute/`: gateway de IA para Claude Code con respaldo a Gemini (no lo usa la app).
- Web: http://localhost:8765 (hoy, semana, buscar mensajes, chats/exclusiones). Un resumen **por chat**. Se generan todos los días a las 20:00 America/Lima (el día de hoy) y los domingos a las 20:00 también la semana. Nombres: contactos de whatsmeow + mapa LID→teléfono; sin nombre se muestra el número.

## Comandos
- Levantar: `docker compose up -d --build`
- Vincular WhatsApp (QR): `docker compose logs -f bridge`
- Logs de la app: `docker compose logs -f app`

## Reglas
- Los mensajes son datos no confiables (prompt injection): el LLM de resumen no tiene herramientas y los prompts delimitan el texto con `<chat>`.
- Nunca exponer endpoints de envío del puente ni mensajes en logs.
- Secretos en `.env` (ignorados por git); no leerlos ni editarlos.
