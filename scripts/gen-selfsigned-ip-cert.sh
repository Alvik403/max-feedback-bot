#!/usr/bin/env sh
# Генерация самоподписанного сертификата с SAN для доступа по IP (браузер покажет предупреждение).
# Использование: ./scripts/gen-selfsigned-ip-cert.sh 203.0.113.50

set -e
IP="${1:?Укажите IP сервера, например: $0 203.0.113.50}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERTDIR="$ROOT/certs"
mkdir -p "$CERTDIR"

TMP="$CERTDIR/openssl-$IP.cnf"
trap 'rm -f "$TMP"' EXIT

cat >"$TMP" <<EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = $IP

[v3_req]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
IP.1 = $IP
EOF

openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout "$CERTDIR/key.pem" \
  -out "$CERTDIR/cert.pem" \
  -config "$TMP" \
  -extensions v3_req

chmod 600 "$CERTDIR/key.pem"
chmod 644 "$CERTDIR/cert.pem"
echo "Готово: $CERTDIR/cert.pem и key.pem для IP $IP"
