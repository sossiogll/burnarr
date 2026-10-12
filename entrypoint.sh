#!/bin/sh
# Burnarr entrypoint:
#   - allinea l'utente "burnarr" a PUID/PGID
#   - lo aggiunge al gruppo proprietario del device /dev/sr*
#   - prepara /data e timezone
#   - esegue il comando come utente non privilegiato (gosu)
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}
TZ=${TZ:-UTC}
DRIVE_DEVICE=${DRIVE_DEVICE:-/dev/sr0}

# ---------- Timezone ----------
if [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -sf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

# ---------- Gruppo + utente ----------
if ! getent group burnarr >/dev/null 2>&1; then
    groupadd -g "$PGID" burnarr 2>/dev/null \
        || groupadd -o -g "$PGID" burnarr 2>/dev/null \
        || groupadd burnarr
else
    groupmod -o -g "$PGID" burnarr 2>/dev/null || true
fi

if ! getent passwd burnarr >/dev/null 2>&1; then
    useradd -o -u "$PUID" -g burnarr -M -d /app -s /sbin/nologin burnarr 2>/dev/null \
        || useradd -g burnarr -M -d /app -s /sbin/nologin burnarr
else
    usermod -o -u "$PUID" burnarr 2>/dev/null || true
fi

# ---------- Accesso al drive ottico ----------
# Il device viene esposto con i permessi del gruppo dell'host (di solito cdrom o
# disk, gid 24 o 6). Creiamo il gruppo corrispondente dentro al container e ci
# mettiamo l'utente burnarr.
if [ -e "$DRIVE_DEVICE" ]; then
    DEVGID=$(stat -c '%g' "$DRIVE_DEVICE" 2>/dev/null || echo "")
    if [ -n "$DEVGID" ] && [ "$DEVGID" != "0" ]; then
        GROUPNAME=$(getent group "$DEVGID" | cut -d: -f1)
        if [ -z "$GROUPNAME" ]; then
            GROUPNAME="optical"
            groupadd -o -g "$DEVGID" "$GROUPNAME" 2>/dev/null || true
        fi
        usermod -aG "$GROUPNAME" burnarr 2>/dev/null || true
        echo "[entrypoint] $DRIVE_DEVICE owned by gid=$DEVGID ($GROUPNAME); burnarr added to group."
    fi
    # Permessi di lettura/scrittura sul device (gosu non passa per il gruppo se
    # i permessi sono 0660 e il gruppo non è quello giusto: già gestito sopra,
    # ma garantiamo comunque 0660).
    chmod 0660 "$DRIVE_DEVICE" 2>/dev/null || true
else
    echo "[entrypoint] ATTENZIONE: $DRIVE_DEVICE non trovato. Controlla la sezione 'devices' in docker-compose.yml."
fi

# ---------- /data ----------
mkdir -p /data/uploads /data/logs
# /data/iso è bind-mount read-only, non toccarlo.
chown -R burnarr:burnarr /data/uploads /data/logs 2>/dev/null || true
if [ -f /data/history.db ]; then
    chown burnarr:burnarr /data/history.db 2>/dev/null || true
fi

echo "[entrypoint] avvio come burnarr (uid=$PUID gid=$PGID)"
exec gosu burnarr "$@"
