"""Stage file edits in a scratch copy until stage_apply."""

from __future__ import annotations

import json
from pathlib import Path

from .engine import (
    COMMAND_TOOLS,
    STAGED_TOOLS,
    _is_v4a,
    parse_paths_parameter,
    patch_call_failed,
    plan_tool,
    settle_v4a_batch,
    stage_apply,
    stage_diff,
    stage_discard,
    stage_receipts,
    stage_status,
)

_SCRATCH_NOTE = (
    "\nstage: scratch copy only. The original file is unchanged until stage_apply. "
    "Call stage_diff, then stage_apply or stage_discard."
)


def register(ctx):
    def before(tool_name, args=None, task_id="", tool_call_id="", **kwargs):
        del kwargs
        try:
            return plan_tool(tool_name, args or {}, task_id, tool_call_id=tool_call_id)
        except Exception as exc:
            if tool_name in STAGED_TOOLS or tool_name in COMMAND_TOOLS:
                return {
                    "action": "block",
                    "message": (
                        "stage: could not stage this write "
                        f"({type(exc).__name__}). The original file was not changed."
                    ),
                }
            return None

    def rewrite_result(tool_name, args=None, result=None, task_id="", status="", error_type="", tool_call_id="", **kwargs):
        del kwargs
        try:
            _settle_patch(
                tool_name, args=args, result=result, status=status, error_type=error_type,
                task_id=task_id, tool_call_id=tool_call_id,
            )
            if tool_name not in STAGED_TOOLS or not isinstance(result, str):
                return None
            if "scratch copy only" in result:
                return None
            if "plugin-data/stage/sessions/" not in result:
                return None
            return result + _SCRATCH_NOTE
        except Exception:
            return None

    def after_tool(tool_name, args=None, result=None, task_id="", status="", error_type="", tool_call_id="", **kwargs):
        del kwargs
        try:
            _settle_patch(
                tool_name, args=args, result=result, status=status, error_type=error_type,
                task_id=task_id, tool_call_id=tool_call_id,
            )
        except Exception:
            return None
        return None

    ctx.register_hook("pre_tool_call", before)
    ctx.register_hook("post_tool_call", after_tool)
    ctx.register_hook("transform_tool_result", rewrite_result)

    ctx.register_tool(
        name="stage_status",
        toolset="stage",
        schema=_schema(
            "stage_status",
            "List files whose writes are sitting in the scratch copy for this task. Originals are unchanged.",
        ),
        handler=_status,
        description="List staged file edits that have not been applied.",
    )
    ctx.register_tool(
        name="stage_diff",
        toolset="stage",
        schema=_paths_schema(
            "stage_diff",
            "Show the unified diff between each original file and its scratch copy. Originals are unchanged.",
        ),
        handler=_diff,
        description="Show staged diffs without changing original files.",
    )
    ctx.register_tool(
        name="stage_apply",
        toolset="stage",
        schema=_paths_schema(
            "stage_apply",
            "Copy staged files onto their original paths. Optional paths applies a subset. Refuses a file whose bytes changed after staging. This is the only call that changes the originals.",
        ),
        handler=_apply,
        description="Apply staged file edits onto the original files.",
    )
    ctx.register_tool(
        name="stage_discard",
        toolset="stage",
        schema=_paths_schema(
            "stage_discard",
            "Delete scratch copies. Optional paths discards a subset. Original files stay as they are.",
        ),
        handler=_discard,
        description="Throw away staged file edits.",
    )
    ctx.register_tool(
        name="stage_receipts",
        toolset="stage",
        schema={
            "name": "stage_receipts",
            "description": "List recent apply receipts with before and after SHA-256 hashes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many recent receipts to return. Default 10.",
                    },
                },
                "required": [],
            },
        },
        handler=_receipts,
        description="List recent stage_apply receipts.",
    )

    skill = Path(__file__).parent / "skills" / "stage" / "SKILL.md"
    ctx.register_skill("stage", skill, description=_SKILL_DESCRIPTION)


def _settle_patch(tool_name, args=None, result=None, status="", error_type="", task_id="", tool_call_id=""):
    payload = args if isinstance(args, dict) else {}
    if tool_name != "patch":
        return
    if payload and not _is_v4a(payload):
        return
    failed = patch_call_failed(status, error_type, result)
    settle_v4a_batch(task_id, failed=failed, tool_call_id=tool_call_id)


def _paths_or_error(args, *, empty_result: dict):
    paths, error = parse_paths_parameter(args)
    if error:
        payload = dict(empty_result)
        payload["errors"] = [{"path": "", "error": error}]
        return None, json.dumps(payload)
    return paths, None


def _status(args, task_id="", **kwargs):
    del args, kwargs
    return json.dumps(stage_status(task_id))


def _diff(args, task_id="", **kwargs):
    del kwargs
    paths, payload = _paths_or_error(args, empty_result={"files": [], "errors": []})
    if payload is not None:
        return payload
    return json.dumps(stage_diff(task_id, paths))


def _apply(args, task_id="", **kwargs):
    del kwargs
    paths, payload = _paths_or_error(args, empty_result={"applied": [], "errors": [], "receipts": []})
    if payload is not None:
        return payload
    return json.dumps(stage_apply(task_id, paths))


def _discard(args, task_id="", **kwargs):
    del kwargs
    paths, payload = _paths_or_error(args, empty_result={"discarded": [], "errors": []})
    if payload is not None:
        return payload
    return json.dumps(stage_discard(task_id, paths))


def _receipts(args, task_id="", **kwargs):
    del task_id, kwargs
    limit = 10
    if isinstance(args, dict) and isinstance(args.get("limit"), int):
        limit = args["limit"]
    return json.dumps(stage_receipts(limit))


def _schema(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {}, "required": []},
    }


def _paths_schema(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Workspace-relative paths. Omit to include every pending file. An empty array does nothing.",
                },
            },
            "required": [],
        },
    }


_SKILL_DESCRIPTION = (
    "Use after write_file or patch, and when the user says to apply or discard. "
    "Those writes land in a scratch copy; the original file changes only on stage_apply. "
    "Call stage_diff before apply."
)
