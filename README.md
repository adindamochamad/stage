# stage

Hermes `write_file` and `patch` land in a scratch copy. The original file stays as it is until you apply.

## Install

From this directory:

```bash
mkdir -p ~/.hermes/plugins
ln -sfn "$PWD" ~/.hermes/plugins/stage
hermes plugins enable stage
```

Start a new Hermes session after enabling. The plugin uses the Python standard library only.

## Example

In a Hermes session, with `notes.txt` and `draft.txt` in the workspace:

> Rewrite notes.txt so the first line says the relay is on. Rewrite draft.txt the same way. Leave both staged.

Hermes writes scratch copies. Both files on disk still have the old lines. Then:

> Show the diffs. Apply notes.txt. Discard draft.txt.

`stage_diff` prints the pending edits. `stage_apply` with `paths: ["notes.txt"]` copies that scratch onto `notes.txt` and returns a receipt with the SHA-256 before and after. `stage_discard` with `paths: ["draft.txt"]` throws that scratch away. `draft.txt` on disk is unchanged.

## Demo beat (under 90 seconds)

1. Ask Hermes to rewrite a file and leave it staged.
2. Hermes reports the write. `cat` the file: the old line is still there.
3. `stage_diff` shows the new line in the scratch copy.
4. `stage_apply` updates the file. The receipt has two hashes.

## Errors

1. **Path outside the workspace.** A write to `/tmp` or another directory is blocked. The destination file is not created.
2. **`patch` on a file that is not there.** The call is blocked. Nothing is created and nothing else changes.
3. **`stage_apply` when nothing is staged.** The tool returns an empty `applied` list. Files on disk stay as they are.
4. **`stage_apply` after the original changed.** The apply for that file is refused. The newer bytes stay, and the scratch copy stays pending. Call `stage_diff` for that path, then `stage_discard` on that path. Staging the same path again does not refresh the protected hash.
5. **Shell or `execute_code` that would change a workspace file.** The plugin blocks common shell patterns (`sed -i`, redirects into the workspace, `rm`/`mv` touching the workspace, `cp` whose destination is in the workspace). Use `write_file` or `patch`, then `stage_apply`.

## Known limits

- MCP tools are not staged.
- File deletion is staged only for V4A `Delete File` hunks. Other delete tools still change disk. A mixed V4A patch (delete plus add/update) is committed only after the Hermes patch succeeds; if that patch fails, the delete is dropped and any earlier staged edit of those paths is restored. Uncommitted patch batches appear in `stage_status` with `committed: false` and are not applied. A parallel V4A patch on the same path is rejected until the previous batch is committed or discarded.
- `read_file` reads the original file, not the scratch copy.
- A scratch journal written by 0.1.0 has no hash, so apply refuses it. Call `stage_discard` before a demo.
- Shell commands that hide a write (`python -c`, `dd`) are not all caught.

## What it touches

No network, no credentials, and no background process. Scratch copies, the journal, and apply receipts live under `~/.hermes/plugin-data/stage/`. `stage_apply` copies those files onto the original paths inside the workspace, or deletes an original that was staged as a V4A delete. `stage_discard` deletes only the scratch copy.

## Check

```bash
hermes plugins validate . --install-deps
```
