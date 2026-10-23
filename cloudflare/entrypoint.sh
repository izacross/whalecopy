#!/bin/sh
# Container start script for Cloudflare: restore the database from R2, then run the bot
# under Litestream so every write is copied back to R2.
set -eu

for v in R2_BUCKET R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  eval "val=\${$v:-}"
  if [ -z "$val" ]; then
    echo "missing $v: without R2 the database would be lost on every restart" >&2
    exit 1
  fi
done

# R2_ENDPOINT can be overridden to point at any S3-compatible store (used for local tests).
export R2_ENDPOINT="${R2_ENDPOINT:-https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com}"

DB="${DATA_DIR:-/data}/whalecopy.db"
if [ "$DB" != "/data/whalecopy.db" ]; then
  echo "DATA_DIR must be /data on Cloudflare (litestream.yml replicates /data/whalecopy.db)" >&2
  exit 1
fi

# First boot has no replica yet; later boots pull the latest copy.
litestream restore -config /etc/litestream.yml -if-db-not-exists -if-replica-exists "$DB"

# Litestream runs the bot as a child and exits when it does.
exec litestream replicate -config /etc/litestream.yml -exec "whalecopy run"
