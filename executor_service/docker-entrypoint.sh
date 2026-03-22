#!/bin/sh
set -eu

mkdir -p /run/isolate/locks

if [ -d /sys/fs/cgroup ]; then
    cg_rel="$(awk -F: '$1 == "0" && $2 == "" {print $3}' /proc/self/cgroup 2>/dev/null || true)"
    cg_root="/sys/fs/cgroup${cg_rel:-}"

    if [ -n "$cg_rel" ] && [ -d "$cg_root" ]; then
        mkdir -p "$cg_root/daemon" 2>/dev/null || true
        printf '%s\n' "$$" > "$cg_root/daemon/cgroup.procs" 2>/dev/null || true
        printf '+cpuset +memory\n' > "$cg_root/cgroup.subtree_control" 2>/dev/null || true
        printf '%s\n' "$cg_root" > /run/isolate/cgroup
    fi
fi

exec "$@"
