---
name: stage
description: "Use after write_file or patch, and when the user says to apply or discard. Those writes land in a scratch copy; the original file changes only on stage_apply."
version: 0.2.0
author: Adinda
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [files, stage, safety]
---

# stage

`write_file` and `patch` do not change the original file while this plugin is on. Hermes writes a scratch copy. The original bytes stay until `stage_apply`. If those bytes change after staging, `stage_apply` refuses that file and leaves the newer bytes in place. A later `write_file` or `patch` on the same path does not refresh that hash. Call `stage_diff` for that path, then `stage_discard` on that path.

Do not use `terminal` or `execute_code` to edit a file. Those calls are blocked when they would change a workspace file. Use `write_file` or `patch`, then `stage_apply`.

## When this applies

- You just called `write_file` or `patch`.
- The user asks what changed, or says apply or discard.
- A tool result contains `scratch copy only`.

## What to tell the user

Say that the edit is staged and the original file is unchanged. Call `stage_diff` and report the pending paths and the diff. Ask before `stage_apply` unless the user already said to apply.

## Commands

- `stage_status` lists pending files with added/removed line counts and conflict flags.
- `stage_diff` shows the unified diff for pending files. Pass `paths` to limit the list.
- `stage_apply` copies the scratch onto the original paths. Pass `paths` to apply a subset. This is the step that changes disk. A successful apply returns a receipt with before and after SHA-256 hashes.
- `stage_discard` deletes the scratch. Pass `paths` to discard a subset. Originals stay as they were.
- `stage_receipts` lists recent apply receipts.

A path outside the workspace is blocked. A `patch` of a file that does not exist yet is blocked. Neither one changes an original file.
