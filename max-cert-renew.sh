#!/usr/bin/env bash
# Existing MAX IP and optional admin domain certificates. Never change VPN rules.
set -Eeuo pipefail
cd -- "${MAX_CERT_PROJECT_DIR:-$(dirname -- "${BASH_SOURCE[0]}")}"
project_dir=$PWD
cert_root=$project_dir/certs/max-edge
cert_file=$cert_root/letsencrypt/live/bible-max-ip/cert.pem
nginx_image=nginx@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94
certbot_image=certbot/certbot@sha256:f70ad0adbb7e117f0fe42a63c553f28ea451edabc0148757b6efcd9735acaa20
dry_run=()
if [[ $# -gt 0 ]]; then
    [[ $# -eq 1 && $1 == --dry-run ]] || { echo 'Usage: max-cert-renew.sh [--dry-run]' >&2; exit 2; }
    dry_run=(--dry-run)
fi
[[ -f $cert_file && -d $cert_root/webroot ]] || { echo 'Prepare the IP certificate first.' >&2; exit 1; }
exec 9>"$cert_root/renew.lock"
flock -n 9 || { echo 'Certificate maintenance is already running.' >&2; exit 1; }
mkdir -p "$cert_root/webroot/.well-known/acme-challenge"
chmod 755 "$cert_root/webroot/.well-known" "$cert_root/webroot/.well-known/acme-challenge"

container=bible-max-acme
owner_label=io.bible-messenger.role
if docker container inspect "$container" >/dev/null 2>&1; then
    owner=$(docker inspect --format '{{index .Config.Labels "io.bible-messenger.role"}}' "$container")
    [[ $owner == max-acme ]] || { echo 'Refusing to reuse an unowned container.' >&2; exit 1; }
    if [[ $(docker inspect --format '{{.State.Running}}' "$container") != true ]]; then
        docker start "$container" >/dev/null
    fi
else
    # A port conflict is an error, never a reason to stop another service.
    python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('0.0.0.0', 80))
PY
    docker run --rm --network none \
        -v "$project_dir/deploy/max-acme.nginx.conf:/etc/nginx/nginx.conf:ro" \
        "$nginx_image" nginx -t
    docker run -d --name "$container" --label "$owner_label=max-acme" \
        --restart unless-stopped --network host --memory 128m --cpus 1 \
        --security-opt no-new-privileges:true --cap-drop ALL \
        --cap-add NET_BIND_SERVICE --cap-add SETUID --cap-add SETGID --cap-add CHOWN \
        --health-cmd 'wget -q -O - http://127.0.0.1/max-edge-health || exit 1' \
        --health-interval 30s --health-timeout 5s --health-retries 3 \
        -v "$project_dir/deploy/max-acme.nginx.conf:/etc/nginx/nginx.conf:ro" \
        -v "$cert_root/webroot:/var/www/certbot:ro" "$nginx_image" >/dev/null
fi
for attempt in {1..15}; do
    if curl -fsS --max-time 2 http://127.0.0.1/max-edge-health >/dev/null; then break; fi
    sleep 1
done
curl -fsS --max-time 2 http://127.0.0.1/max-edge-health >/dev/null

cert_names=(bible-max-ip)
if [[ -f $cert_root/letsencrypt/renewal/bible-admin-domain.conf ]]; then
    cert_names+=(bible-admin-domain)
fi
for cert_name in "${cert_names[@]}"; do
docker run --rm --memory 256m --cpus 0.5 \
    -v "$cert_root/webroot:/var/www/certbot" \
    -v "$cert_root/letsencrypt:/etc/letsencrypt" \
    -v "$cert_root/acme-work:/var/lib/letsencrypt" \
    -v "$cert_root/acme-logs:/var/log/letsencrypt" \
    "$certbot_image" renew --non-interactive --cert-name "$cert_name" "${dry_run[@]}"
done

# Dry-run never installs a staging certificate or reloads a live TLS service.
if [[ ${#dry_run[@]} -gt 0 ]]; then
    echo 'ACME renewal simulation succeeded; live TLS unchanged.'
    exit 0
fi
openssl x509 -in "$cert_file" -checkend 86400 -noout
if [[ ${#cert_names[@]} -gt 1 ]]; then
    openssl x509 -in "$cert_root/letsencrypt/live/bible-admin-domain/cert.pem" -checkend 86400 -noout
fi

# Reload on every successful normal run, even if the preceding reload failed.
# This also retries installation when Certbot says the cert is not yet due.
gateway=bible-max-edge
if docker container inspect "$gateway" >/dev/null 2>&1; then
    owner=$(docker inspect --format '{{index .Config.Labels "io.bible-messenger.role"}}' "$gateway")
    [[ $owner == max-edge ]] || { echo 'Refusing to reload an unowned container.' >&2; exit 1; }
    [[ $(docker inspect --format '{{.State.Running}}' "$gateway") == true ]] || {
        echo 'Configured MAX TLS gateway is stopped.' >&2; exit 1;
    }
    docker exec "$gateway" nginx -t
    docker exec "$gateway" nginx -s reload
else
    echo 'Certificate maintained; public MAX TLS gateway is not activated yet.'
fi
