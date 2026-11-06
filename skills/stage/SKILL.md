---
name: stage
description: "Use after write_file or patch, and when the user says to apply or discard. Those writes land in a scratch copy; the original file changes only on stage_apply."
version: 0.1.6
author: Adinda
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [files, stage, safety]
---

# stage

`write_file` and `patch` do not change the original file while this plugin is on. Hermes writes a scratch copy. The original bytes stay until `stage_apply`. If those bytes change after staging, `stage_apply` refuses that file and leaves the newer bytes in place. A later `write_file` or `patch` on the same path does not refresh that hash. Call `stage_discard` when apply refuses.

## When this applies

- You just called `write_file` or `patch`.
- The user asks what changed, or says apply or discard.
- A tool result contains `scratch copy only`.

`terminal` is not staged. Do not use the shell to edit a file in order to skip the scratch copy.

## What to tell the user

Say that the edit is staged and the original file is unchanged. Then call `stage_status` and report the pending paths. Ask before `stage_apply` unless the user already said to apply.

## Commands

- `stage_status` lists pending files.
- `stage_apply` copies the scratch onto the original paths. This is the step that changes disk.
- `stage_discard` deletes the scratch. Originals stay as they were.

A path outside the workspace is blocked. A `patch` of a file that does not exist yet is blocked. Neither one changes an original file.
