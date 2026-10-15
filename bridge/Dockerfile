# Puente de WhatsApp (lharries/whatsapp-mcp, solo la parte Go), fijado a un commit.
FROM golang:1.26-bookworm AS build
ARG REF=7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88
RUN git clone https://github.com/lharries/whatsapp-mcp /src && cd /src && git checkout "$REF"
WORKDIR /src/whatsapp-bridge
# El whatsmeow fijado en el repo está desactualizado: WhatsApp rechaza ese cliente (405).
# Se actualiza y se adapta main.go a los cambios de API.
COPY patch-whatsmeow.sh /patch-whatsmeow.sh
RUN go get go.mau.fi/whatsmeow@latest && go mod tidy && bash /patch-whatsmeow.sh \
    && CGO_ENABLED=1 go build -o /bridge .

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /bridge /usr/local/bin/bridge
# Los timestamps se guardan en UTC; la app los convierte a hora de Lima.
ENV TZ=UTC
CMD ["bridge"]
