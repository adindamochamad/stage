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

In a Hermes session, with a file named `notes.txt` in the workspace:

> Rewrite notes.txt so the first line says the relay is on. Leave it staged.

Hermes writes the scratch copy and the file on disk still has the old line. Then:

> Apply the stage.

`stage_apply` copies the scratch onto `notes.txt`. `stage_discard` deletes the scratch and leaves `notes.txt` alone.

## Errors

1. **Path outside the workspace.** A write to `/tmp` or another directory is blocked. The destination file is not created.
2. **`patch` on a file that is not there.** The call is blocked. Nothing is created and nothing else changes.
3. **`stage_apply` when nothing is staged.** The tool returns an empty `applied` list. Files on disk stay as they are.
4. **`stage_apply` after the original changed.** The apply for that file is refused. The newer bytes stay, and the scratch copy stays pending.

## Known limits

- `terminal`, `execute_code`, MCP tools, and file deletion are not staged. Any of those can still change a file directly.
- One staging session is kept per workspace. There is no multi-process lock.
- `read_file` reads the original file, not the scratch copy.
- If apply reports that the original changed, call `stage_discard`. Staging the same path again does not refresh the protected hash.
- A scratch journal written by 0.1.0 has no hash, so apply refuses it. Call `stage_discard` before a demo.
- **V4A multi-file patch is not supported.** The plugin only stages single-file `write_file` and `patch` calls. V4A format patches that modify multiple files in one call are always blocked.

## What it touches

No network, no credentials, and no background process. Scratch copies and the journal live under `~/.hermes/plugin-data/stage/`. `stage_apply` copies those files onto the original paths inside the workspace. `stage_discard` deletes only the scratch copy.

## Check

```bash
hermes plugins validate . --install-deps
```
