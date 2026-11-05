"""Happy path against Hermes itself. Run with the Hermes runtime Python."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
HOME = Path("/tmp/hermes-stage-happy")
WORK = HOME / "workspace"
NOTE = WORK / "note.txt"


def _call(name: str, args: dict, task_id: str) -> str:
    from model_tools import handle_function_call

    return handle_function_call(name, args, task_id=task_id)


def _load(result: str) -> dict:
    return json.loads(result)


def main() -> int:
    os.chdir(WORK)
    NOTE.write_text("OLD\n", encoding="utf-8")

    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load()
    plugin = manager._plugins.get("stage")
    if plugin is None or plugin.error:
        print("FAIL plugin", None if plugin is None else plugin.error)
        return 1

    written = _call("write_file", {"path": "note.txt", "content": "NEW\n"}, "happy")
    if NOTE.read_text(encoding="utf-8") != "OLD\n":
        print("FAIL write changed the original", NOTE.read_text(encoding="utf-8"))
        return 1
    if "scratch copy only" not in written:
        print("FAIL write result missing scratch note", written[:400])
        return 1

    status = _load(_call("stage_status", {}, "happy"))
    if [row["path"] for row in status["pending"]] != ["note.txt"]:
        print("FAIL status after write", status)
        return 1

    discarded = _load(_call("stage_discard", {}, "happy"))
    if discarded["discarded"] != ["note.txt"] or NOTE.read_text(encoding="utf-8") != "OLD\n":
        print("FAIL discard", discarded, NOTE.read_text(encoding="utf-8"))
        return 1
    if _load(_call("stage_status", {}, "happy"))["pending"]:
        print("FAIL status after discard")
        return 1

    _call("write_file", {"path": "note.txt", "content": "APPLIED\n"}, "happy")
    applied = _load(_call("stage_apply", {}, "happy"))
    if applied["applied"] != ["note.txt"] or NOTE.read_text(encoding="utf-8") != "APPLIED\n":
        print("FAIL apply", applied, NOTE.read_text(encoding="utf-8"))
        return 1

    patched = _call(
        "patch",
        {"path": "note.txt", "old_string": "APPLIED", "new_string": "PATCHED"},
        "happy",
    )
    if NOTE.read_text(encoding="utf-8") != "APPLIED\n" or "scratch copy only" not in patched:
        print("FAIL patch", NOTE.read_text(encoding="utf-8"), patched[:400])
        return 1
    applied = _load(_call("stage_apply", {}, "happy"))
    if applied["applied"] != ["note.txt"] or NOTE.read_text(encoding="utf-8") != "PATCHED\n":
        print("FAIL patch apply", applied, NOTE.read_text(encoding="utf-8"))
        return 1

    missing = _call("patch", {"path": "missing.txt", "old_string": "a", "new_string": "b"}, "happy")
    if "existing file" not in missing or (WORK / "missing.txt").exists():
        print("FAIL missing patch", missing[:400])
        return 1

    outside = HOME / "outside.txt"
    blocked = _call("write_file", {"path": str(outside), "content": "NO\n"}, "happy")
    if "outside" not in blocked or outside.exists():
        print("FAIL outside", blocked[:400])
        return 1

    empty = _load(_call("stage_apply", {}, "other-task"))
    if empty["applied"] or NOTE.read_text(encoding="utf-8") != "PATCHED\n":
        print("FAIL empty apply", empty)
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
