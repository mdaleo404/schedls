# Architecture

`schedls` is a thin CLI over two native scheduling backends. It holds no state
of its own: the scheduler configuration on the host is the source of truth.

```text
parse args
    |
    v
validate request
    |
    v
operation / service layer      operations/
    |
    v
backend                        backends/systemd.py, backends/cron.py
    |
    v
result model                   models.py
    |
    v
formatter                      output.py
```

## Package layout

```text
src/schedls/
  __init__.py        version
  __main__.py        python -m schedls
  cli.py             argparse, dispatch, request building
  errors.py          exception hierarchy and exit codes
  models.py          common model (jobs, specs, schedules, commands)
  output.py          human tables, key/value views, JSON schema
  runner.py          the only place subprocesses are executed
  security.py        name validation, trusted paths, atomic writes
  timefmt.py         durations, timestamps, formatting
  describe.py        presentation-only schedule descriptions
  convenience.py     convenience flags -> native expressions
  unitfile.py        read-only systemd unit-file parser
  interact.py        confirmation handling
  prompt.py          opt-in interactive input collection (wizard)
  backends/
    base.py          capability model, plans, mutation results
    systemd.py       discovery + transactional mutation
    cron.py          lossless crontab model + discovery + mutation
  renderers/
    systemd.py       ExecStart quoting, unit rendering, ExecStart parsing
    cron.py          shell-safe command rendering, managed blocks
  operations/
    inspect.py       collect, filter, list, show
    calendar.py      schedls calendar
    doctor.py        schedls doctor
    mutate.py        preview, confirm, apply
```

## Layers

- **Backends** implement discovery and mutation against native facilities and
  never format output.
- **Renderers** are pure functions from an internal model to native text. They
  are the most security-sensitive code and are covered by exhaustive tests.
- **Operations** orchestrate: gather, preview, confirm, apply.
- **CLI** translates arguments into a request, calls one operation, and maps
  errors to exit codes. With `--interactive` it first fills missing fields via
  **prompt** (line-based, terminal-only), then continues down the normal path.
- **Runner** is the single process-execution boundary. `shell=True` never
  appears in production code.

## Mutation plans

Mutating commands do not write directly. A backend produces a `Plan`:

```text
Plan
  action      create | update | remove | enable | disable
  summary     key/value lines for the preview
  files       FileChange(path, content | None)
  commands    CommandPlan(argv, description, input_text)
  warnings    messages to show the user
  payload     backend-private state (scope, unit names, snapshots)
```

The caller previews the plan, obtains confirmation, then calls `apply`. Plans
snapshot existing content so a failed step can be rolled back. Cron plans also
record the previous crontab text and re-check it at apply time to detect
concurrent edits.

## Exit codes

```text
0  success
1  operational failure
2  invalid command-line input or invalid schedule
3  safety refusal / conflict
```

## JSON schema

`schedls --json` emits a single document on stdout; diagnostics go to stderr.
The schema version is `1`.

```json
{
  "schema_version": 1,
  "jobs": [
    {
      "name": "backup",
      "backend": "systemd",
      "scope": "user",
      "managed": true,
      "enabled": true,
      "schedule": {"kind": "calendar", "expression": "*-*-* 02:00:00"},
      "command": {"argv": ["/usr/local/bin/backup"], "shell": false, "raw": null},
      "source": {"detail": "systemd user timer", "path": "...", "line": null},
      "next_run": "2026-09-25T02:00:00+01:00",
      "last_run": null,
      "last_result": null,
      "warnings": []
    }
  ],
  "warnings": []
}
```

Rules: no ANSI sequences; ISO 8601 timestamps with explicit offsets; stable
field names; missing data is `null`, never invented.
