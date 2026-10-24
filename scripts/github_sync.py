#!/usr/bin/env python3
"""Plan a GitHub Contents-API sync for a local tree, without any network access.

The sandbox used to author this repository cannot run `git push` (pipe creation is
denied) and Node child-process pipes are restricted, so the push is executed by
small `gh api` calls driven from bash. This helper does the parts that are awkward
in bash: git blob hashes, base64 payloads, and the create/update/delete plan.

Usage:
  github_sync.py plan <root> <remote-tree.tsv> <plan-dir>

`remote-tree.tsv` lines are "<path>\t<blob-sha>"; an empty file means the remote
repository has no commits yet. The plan is written as:
  <plan-dir>/puts.tsv      path \t payload-json-path
  <plan-dir>/deletes.tsv   path \t remote-sha
  <plan-dir>/payloads/*.json
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys

SKIP_DIRS = {".git", "node_modules", "evidence", "dist", "work", "__pycache__", ".desktop-build", ".sync-work", ".selftest-tmp"}
SKIP_FILES = {".DS_Store"}


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def local_files(root: str) -> list[str]:
    found: list[str] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for filename in sorted(filenames):
            if filename in SKIP_FILES:
                continue
            absolute = os.path.join(directory, filename)
            relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            found.append(relative)
    return sorted(found)


def read_remote_tree(path: str) -> dict[str, str]:
    remote: dict[str, str] = {}
    if not os.path.exists(path):
        return remote
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            name, _, sha = line.partition("\t")
            if sha:
                remote[name] = sha
    return remote


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] != "plan":
        print(__doc__)
        return 2
    root, remote_tree_path, plan_dir = sys.argv[2], sys.argv[3], sys.argv[4]
    os.makedirs(plan_dir, exist_ok=True)
    payload_dir = os.path.join(plan_dir, "payloads")
    os.makedirs(payload_dir, exist_ok=True)

    remote = read_remote_tree(remote_tree_path)
    local = local_files(root)

    puts: list[tuple[str, str]] = []
    deletes: list[tuple[str, str]] = []
    unchanged = 0

    for index, relative in enumerate(local):
        absolute = os.path.join(root, relative)
        with open(absolute, "rb") as handle:
            content = handle.read()
        blob = git_blob_sha(content)
        if remote.get(relative) == blob:
            unchanged += 1
            continue
        payload = {
            "message": f"Sync Linux arm64 build pipeline: {relative}",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": os.environ.get("SYNC_BRANCH", "main"),
        }
        if relative in remote:
            payload["sha"] = remote[relative]
        payload_path = os.path.join(payload_dir, f"{index:04d}.json").replace(os.sep, "/")
        with open(payload_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        puts.append((relative, payload_path))

    for relative, sha in sorted(remote.items()):
        if relative not in local:
            deletes.append((relative, sha))

    with open(os.path.join(plan_dir, "puts.tsv"), "w", encoding="utf-8", newline="\n") as handle:
        for relative, payload_path in puts:
            handle.write(f"{relative}\t{payload_path}\n")
    with open(os.path.join(plan_dir, "deletes.tsv"), "w", encoding="utf-8", newline="\n") as handle:
        for relative, sha in deletes:
            handle.write(f"{relative}\t{sha}\n")

    print(f"plan: {len(puts)} put(s), {len(deletes)} delete(s), {unchanged} unchanged, {len(local)} local file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
