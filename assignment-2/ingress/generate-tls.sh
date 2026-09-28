#!/usr/bin/env bash
# Creates the TLS Secret the Ingress terminates HTTPS with (§2.2.5).
#
# A self-signed certificate is used because tasktracker.local is not a real
# public domain, so no public CA would issue for it. In production this
# script is replaced by cert-manager issuing and renewing the Secret
# automatically (§2.2.6) - the Ingress object would not change at all.
#
# The key never touches the repository: it is written to a temp dir, loaded
# into the cluster, and deleted.
set -euo pipefail

NS=dso202-assignment-02
HOST=tasktracker.local
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout "$TMP/tls.key" -out "$TMP/tls.crt" \
  -subj "/CN=${HOST}/O=DSO202" \
  -addext "subjectAltName=DNS:${HOST}"

# --dry-run=client | apply makes the script idempotent (safe to re-run to rotate).
kubectl create secret tls tasktracker-tls -n "$NS" \
  --cert="$TMP/tls.crt" --key="$TMP/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -

openssl x509 -in "$TMP/tls.crt" -noout -subject -ext subjectAltName -enddate
