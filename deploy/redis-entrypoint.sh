#!/bin/sh
set -eu

# Keep the host's credential file private and read-only. Linux bind mounts
# preserve its owner and mode, so Redis needs a separate private copy before
# the official entrypoint drops privileges to the redis user.
if [ "${1:-}" != "redis-server" ]; then
    echo "Expected redis-server for the isolated Redis service." >&2
    exit 1
fi
shift

umask 077
runtime_directory="$(mktemp -d /tmp/zhihe-redis.XXXXXX)"
runtime_config="$runtime_directory/redis.conf"
cp /etc/redis/redis.conf "$runtime_config"
chown redis:redis "$runtime_directory" "$runtime_config"
chmod 700 "$runtime_directory"
chmod 600 "$runtime_config"

# Retain upstream data-volume setup and privilege dropping. No credentials
# are passed as command-line arguments or written to standard output.
exec /usr/local/bin/docker-entrypoint.sh redis-server "$runtime_config" "$@"
