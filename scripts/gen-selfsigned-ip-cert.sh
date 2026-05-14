#!/usr/bin/env sh
# Генерация самоподписанного сертификата с SAN (браузер покажет предупреждение).
# Примеры:
#   ./scripts/gen-selfsigned-ip-cert.sh 203.0.113.50
#   ./scripts/gen-selfsigned-ip-cert.sh https://194.156.101.169/

set -e
RAW="${1:?Укажите IP или URL, например: $0 194.156.101.169}"

RAW="$(printf '%s' "$RAW" | tr -d '[:space:]')"
case "$RAW" in
  http://*)  RAW="${RAW#http://}" ;;
  https://*) RAW="${RAW#https://}" ;;
esac
RAW="${RAW%%/*}"
# Убрать порт (:443), если указали хост:порт
HOST="${RAW%:*}"
if [ -z "$HOST" ]; then
  echo "Не удалось разобрать хост из аргумента: $1"
  exit 1
fi

# IPv4 или имя (DNS в SAN)
if printf '%s' "$HOST" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
  SAN_BLOCK="IP.1 = $HOST"
else
  SAN_BLOCK="DNS.1 = $HOST"
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERTDIR="$ROOT/certs"
mkdir -p "$CERTDIR"

TMP="$CERTDIR/openssl-req-temp.cnf"
trap 'rm -f "$TMP"' EXIT

cat >"$TMP" <<EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = $HOST

[v3_req]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
$SAN_BLOCK
EOF

openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout "$CERTDIR/key.pem" \
  -out "$CERTDIR/cert.pem" \
  -config "$TMP" \
  -extensions v3_req

chmod 600 "$CERTDIR/key.pem"
chmod 644 "$CERTDIR/cert.pem"
echo "Готово: $CERTDIR/cert.pem и key.pem для $HOST"
