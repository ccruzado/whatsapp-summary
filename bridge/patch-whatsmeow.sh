# Adapta main.go a la API actual de whatsmeow (ahora todas esas llamadas piden context).
set -e
sed -i \
 -e 's/client\.Download(/client.Download(context.Background(), /' \
 -e 's/sqlstore\.New("sqlite3"/sqlstore.New(context.Background(), "sqlite3"/' \
 -e 's/GetFirstDevice()/GetFirstDevice(context.Background())/' \
 -e 's/\.GetGroupInfo(/.GetGroupInfo(context.Background(), /' \
 -e 's/Contacts\.GetContact(/Contacts.GetContact(context.Background(), /' main.go
