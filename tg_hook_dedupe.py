#!/usr/bin/env python3
"""Atomic per-turn delivery guard shared by Codex Stop and notify hooks."""

import hashlib
import json
import os
import sys


MARKER_DIR = "/tmp/claude_tg_delivered_turns"


def _turn_id(payload):
    value = payload.get("turn_id") or payload.get("turn-id")
    return value if isinstance(value, str) and value else None


def claim(payload, marker_dir=MARKER_DIR):
    """Claim a turn atomically; return marker path, None for duplicate, or '' if unkeyed."""
    turn_id = _turn_id(payload)
    if turn_id is None:
        return ""

    os.makedirs(marker_dir, exist_ok=True)
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    marker_path = os.path.join(marker_dir, digest)
    try:
        descriptor = os.open(
            marker_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return None
    os.close(descriptor)
    return marker_path


def release(marker_path, marker_dir=MARKER_DIR):
    """Release a failed claim without allowing paths outside the marker directory."""
    marker_root = os.path.realpath(marker_dir)
    candidate = os.path.realpath(marker_path)
    if os.path.dirname(candidate) != marker_root:
        return False
    try:
        os.unlink(candidate)
    except FileNotFoundError:
        return False
    return True


def main(argv=None):
    args = sys.argv if argv is None else argv
    if len(args) < 2:
        return 2

    if args[1] == "claim":
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, TypeError):
            return 2
        marker_path = claim(payload)
        if marker_path is None:
            return 3
        if marker_path:
            print(marker_path)
        return 0

    if args[1] == "release" and len(args) == 3:
        return 0 if release(args[2]) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
