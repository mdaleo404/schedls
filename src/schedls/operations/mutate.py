"""Generic mutation preview, confirmation and application."""

from __future__ import annotations

from ..backends.base import MutationResult, Plan, SchedulerBackend
from ..interact import Interaction
from ..output import Output

_TITLES = {
    "create": "Create scheduled job",
    "update": "Change scheduled job",
    "remove": "Remove scheduled job",
    "enable": "Enable scheduled job",
    "disable": "Disable scheduled job",
}


def run_plan(
    backend: SchedulerBackend,
    plan: Plan,
    output: Output,
    interaction: Interaction,
    *,
    dry_run: bool = False,
    show_files: bool = False,
) -> MutationResult:
    if not output.json_mode:
        _preview(output, plan, dry_run=dry_run, show_files=show_files)

    if dry_run:
        result = MutationResult(changed=False, dry_run=True, warnings=tuple(plan.warnings))
        if output.json_mode:
            output.emit_json(_plan_document(plan, result))
        else:
            output.line()
            output.line("No changes made.")
        return result

    title = _TITLES.get(plan.action, "Apply changes")
    if not interaction.confirm(f"{title}?"):
        if output.json_mode:
            output.emit_json(_plan_document(plan, MutationResult(changed=False)))
        else:
            output.line("Aborted. No changes made.")
        return MutationResult(changed=False)

    result = backend.apply(plan)
    if output.json_mode:
        output.emit_json(_plan_document(plan, result))
    else:
        for warning in result.warnings:
            output.warning(warning)
        for message in result.messages:
            output.line(message)
    return result


def _plan_document(plan: Plan, result: MutationResult) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": plan.action,
        "backend": plan.backend,
        "changed": result.changed,
        "dry_run": result.dry_run,
        "files": list(result.files_written) or plan.display_files(),
        "commands": [list(command.argv) for command in plan.commands],
        "warnings": list(result.warnings) or list(plan.warnings),
        "messages": list(result.messages),
    }


def _preview(output: Output, plan: Plan, *, dry_run: bool, show_files: bool) -> None:
    title = _TITLES.get(plan.action, "Apply changes")
    output.heading(f"Would {plan.action}:" if dry_run else f"{title}:")

    if plan.summary:
        output.line()
        output.key_values(plan.summary)

    for warning in plan.warnings:
        output.warning(warning)

    created = [change for change in plan.files if change.content is not None]
    removed = [change for change in plan.files if change.content is None]
    if created:
        output.line()
        output.line("Would create:" if dry_run else "Will write:")
        for change in created:
            output.line(f"  {change.path}")
            if show_files and change.content is not None:
                for line in change.content.splitlines():
                    output.line(f"      {line}")
    if removed:
        output.line()
        output.line("Would remove:" if dry_run else "Will remove:")
        for change in removed:
            output.line(f"  {change.path}")

    if plan.commands:
        output.line()
        output.line("Would run:" if dry_run else "Will run:")
        for command in plan.commands:
            output.line(f"  {' '.join(command.argv)}")
