#!/usr/bin/env bash
# Self-signed keypair for the Salesforce Connected App JWT bearer flow.
# server.crt is uploaded to the Connected App; server.key stays secret and is
# what the tool API signs assertions with. Deployed, the key comes from Key Vault.
set -euo pipefail

OUT_DIR="${1:-.secrets}"
DAYS="${DAYS:-3650}"
SUBJECT="${SUBJECT:-/CN=crm-companion-jwt}"

if [[ -e "$OUT_DIR/server.key" ]]; then
    echo "Refusing to overwrite $OUT_DIR/server.key" >&2
    echo "Remove it first if you intend to rotate the key." >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

openssl genrsa -out "$OUT_DIR/server.key" 2048 2>/dev/null
chmod 600 "$OUT_DIR/server.key"

openssl req -new -x509 \
    -key "$OUT_DIR/server.key" \
    -out "$OUT_DIR/server.crt" \
    -days "$DAYS" \
    -subj "$SUBJECT" 2>/dev/null
chmod 644 "$OUT_DIR/server.crt"

echo "Wrote:"
echo "  $OUT_DIR/server.key   private key  (gitignored, never leaves this machine)"
echo "  $OUT_DIR/server.crt   certificate  (upload this to the Connected App)"
echo
echo "Fingerprint:"
openssl x509 -in "$OUT_DIR/server.crt" -noout -fingerprint -sha256
