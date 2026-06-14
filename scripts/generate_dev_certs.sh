#!/usr/bin/env bash
# §1.7 — generate a dev CA + server/client certificate pair for mutually
# authenticated TLS between bank_client and vendor_server_160.
#
# This is a DEV-ONLY certificate authority: the CA private key is written to
# disk alongside the certs it signs. Do not reuse these certs outside local
# development / CI. For a real deployment, replace this script's output with
# certs issued by your organization's CA / a managed PKI, keeping the same
# file names so RunVendorServer160 / BankClient pick them up unchanged.
#
# Output (certs/, gitignored):
#   ca.crt            - CA certificate (root of trust for both peers)
#   ca.key            - CA private key (dev only)
#   server.crt/.key   - vendor_server_160 identity (CN=vendor_server)
#   client.crt/.key   - bank_client identity (CN=bank_client)
#
# Usage:
#   ./scripts/generate_dev_certs.sh [output_dir]   # default: certs/
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$REPO_ROOT/certs}"
DAYS=3650

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

echo "[generate_dev_certs] writing dev CA + certs to $OUT_DIR"

# ── CA ──────────────────────────────────────────────────────────────────
openssl req -x509 -newkey rsa:4096 -sha256 -days "$DAYS" -nodes \
    -keyout ca.key -out ca.crt \
    -subj "/O=PPFDaaS-dev/CN=PPFDaaS-dev-CA" \
    >/dev/null 2>&1

# ── vendor_server_160 identity ─────────────────────────────────────────
openssl req -newkey rsa:4096 -sha256 -nodes \
    -keyout server.key -out server.csr \
    -subj "/O=PPFDaaS-dev/CN=vendor_server" \
    >/dev/null 2>&1

openssl x509 -req -sha256 -days "$DAYS" \
    -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out server.crt \
    -extfile <(printf "subjectAltName=DNS:vendor_server,DNS:localhost,IP:127.0.0.1") \
    >/dev/null 2>&1

# ── bank_client identity ───────────────────────────────────────────────
openssl req -newkey rsa:4096 -sha256 -nodes \
    -keyout client.key -out client.csr \
    -subj "/O=PPFDaaS-dev/CN=bank_client" \
    >/dev/null 2>&1

openssl x509 -req -sha256 -days "$DAYS" \
    -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out client.crt \
    -extfile <(printf "subjectAltName=DNS:bank_client,DNS:localhost,IP:127.0.0.1") \
    >/dev/null 2>&1

rm -f server.csr client.csr ca.srl
chmod 600 ca.key server.key client.key

echo "[generate_dev_certs] done:"
ls -1 "$OUT_DIR"
echo
echo "To enable mTLS:"
echo "  vendor_server_160: set PPFD_TLS_CERT=$OUT_DIR/server.crt PPFD_TLS_KEY=$OUT_DIR/server.key PPFD_TLS_CA=$OUT_DIR/ca.crt"
echo "  BankClient:        BankClient(..., use_tls=True, tls_ca_path='$OUT_DIR/ca.crt', tls_cert_path='$OUT_DIR/client.crt', tls_key_path='$OUT_DIR/client.key')"
