package main

// Endpoint interno (solo red de compose, puerto 8081) que entrega la foto de perfil de un chat.
// Lo consume la app de resúmenes; no envía ni modifica nada en WhatsApp.

import (
	"context"
	"io"
	"net/http"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

func startAvatarServer(client *whatsmeow.Client) {
	mux := http.NewServeMux()
	mux.HandleFunc("/avatar", func(w http.ResponseWriter, r *http.Request) {
		jid, err := types.ParseJID(r.URL.Query().Get("jid"))
		if err != nil {
			http.Error(w, "jid inválido", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		info, err := client.GetProfilePictureInfo(ctx, jid, &whatsmeow.GetProfilePictureParams{Preview: true})
		if err != nil || info == nil || info.URL == "" {
			http.NotFound(w, r)
			return
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, info.URL, nil)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil || resp.StatusCode != http.StatusOK {
			http.NotFound(w, r)
			return
		}
		defer resp.Body.Close()
		w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
		io.Copy(w, resp.Body)
	})
	go http.ListenAndServe(":8081", mux)
}
