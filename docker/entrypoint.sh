#!/bin/sh
set -eu

data_dir="${WATERMARK_DATA_DIR:-/app/data}"

# A bind mount can be created by Docker as root. Prepare it before dropping privileges.
mkdir -p "$data_dir/uploads" "$data_dir/results"
chown -R watermark:watermark "$data_dir"

exec runuser -u watermark -- "$@"
