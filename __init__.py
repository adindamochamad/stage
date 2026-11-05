"""Stage file edits in a scratch copy until stage_apply."""

from __future__ import annotations

import json
from pathlib import Path

from .engine import STAGED_TOOLS, plan_redirect, stage_apply, stage_discard, stage_status

_SCRATCH_NOTE = (
    "\nstage: scratch copy only. The original file is unchanged until stage_apply. "
    "Call stage_status, then stage_apply or stage_discard."
)


def register(ctx):
    def before(tool_name, args=None, task_id="", **kwargs):
        del kwargs
        if tool_name not in STAGED_TOOLS:
            return None
        try:
            payload = args or {}
            return plan_redirect(payload.get("path"), tool_name, task_id)
        except Exception as exc:
            return {
                "action": "block",
                "message": (
                    "stage: could not stage this write "
                    f"({type(exc).__name__}). The original file was not changed."
                ),
            }

    def rewrite_result(tool_name, result=None, **kwargs):
        del kwargs
        try:
            if tool_name not in STAGED_TOOLS or not isinstance(result, str):
                return None
            if "plugin-data/stage/sessions/" not in result:
                return None
            if "scratch copy only" in result:
                return None
            return result + _SCRATCH_NOTE
        except Exception:
            return None

    ctx.register_hook("pre_tool_call", before)
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
        name="stage_apply",
        toolset="stage",
        schema=_schema(
            "stage_apply",
            "Copy every staged file in this task onto its original path. Refuses a file whose bytes changed after staging. This is the only call that changes the originals.",
        ),
        handler=_apply,
        description="Apply staged file edits onto the original files.",
    )
    ctx.register_tool(
        name="stage_discard",
        toolset="stage",
        schema=_schema(
            "stage_discard",
            "Delete the scratch copies for this task. Original files stay as they are.",
        ),
        handler=_discard,
        description="Throw away staged file edits.",
    )

    skill = Path(__file__).parent / "skills" / "stage" / "SKILL.md"
    ctx.register_skill("stage", skill, description=_SKILL_DESCRIPTION)


def _status(args, task_id="", **kwargs):
    del args, kwargs
    return json.dumps(stage_status(task_id))


def _apply(args, task_id="", **kwargs):
    del args, kwargs
    return json.dumps(stage_apply(task_id))


def _discard(args, task_id="", **kwargs):
    del args, kwargs
    return json.dumps(stage_discard(task_id))


def _schema(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {}, "required": []},
    }


_SKILL_DESCRIPTION = (
    "Use after write_file or patch, and when the user says to apply or discard. "
    "Those writes land in a scratch copy; the original file changes only on stage_apply."
)
