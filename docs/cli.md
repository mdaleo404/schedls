# Command-line reference

Complete reference for every `schedls` command, option and exit code. For
background on the backends see [systemd.md](systemd.md) and [cron.md](cron.md);
for the JSON document layout see [architecture.md](architecture.md#json-schema).

## Synopsis

```text
schedls [GLOBAL OPTIONS] [COMMAND] [COMMAND OPTIONS] [-- COMMAND ARGS...]
```

With no command, `schedls` behaves like `schedls list`.

## Global options

Global options may appear before the command. `--user`/`--system`,
`--backend`, `--managed`/`--unmanaged` and `--enabled`/`--disabled` filter what
`list` discovers and prints.

| Option                  | Meaning                                                             |
|-------------------------|---------------------------------------------------------------------|
| `--version`             | print the version and exit                                          |
| `--debug`               | print helper commands and diagnostics on stderr                     |
| `--json`                | emit a single JSON document on stdout (schema version 1)            |
| `--color auto\|always\|never` | colorize human output (default `auto`; `NO_COLOR` disables)   |
| `--utc`                 | display all times in UTC instead of local time                      |
| `--user` / `--system`   | only show user-scope / system-scope jobs                            |
| `--backend systemd\|cron` | only show jobs from one backend                                   |
| `--managed` / `--unmanaged` | only schedls-managed / only unmanaged jobs                      |
| `--enabled` / `--disabled` | only enabled / only disabled jobs                               |

`--json` is intended for scripts. It prints one document on stdout and sends
diagnostics to stderr; no ANSI escapes are emitted.

## Bash completion

Completion is opt-in. After installing schedls, enable it for the current Bash
session with:

```bash
eval "$(register-python-argcomplete schedls)"
```

Add the command to your shell profile to enable it in future sessions. It
completes schedls subcommands, options, and option choices without discovering
schedules.

## Commands

### `list`

```console
$ schedls
$ schedls --system
$ schedls --backend cron
$ schedls --unmanaged --json
```

Lists visible scheduled jobs. Columns are `NAME`, `SCHEDULE`, `NEXT`, `BACKEND`,
`SCOPE`, `STATUS`. A missing next run is shown as `—`. Discovery warnings are
written to stderr as `Note:` lines.

### `show NAME`

```console
$ schedls show backup
```

Shows one job in a field-oriented view: status, backend and scope, whether
schedls manages it, the schedule, next and previous runs, the command, source
paths, and any systemd extras (persistent, jitter, accuracy, working directory,
environment). Human-readable cron next runs are calculated in the host's local
timezone; cron previous runs and results remain unavailable because cron
exposes no per-job execution history. To preserve the JSON document contract,
cron `next_run` remains `null` in `--json` output.

### `new NAME`

```console
$ schedls new backup --timer --daily 02:00 --persistent -- /usr/local/bin/backup /srv/data
$ schedls new cleanup --cron --cron-expr '0 4 * * 0' -- /usr/local/bin/cleanup
$ sudo schedls new updates --cron --system --daily 03:00 -- /usr/local/bin/updates
$ sudo schedls new report --cron --system --daily 04:00 --run-as www-data -- /usr/local/bin/report
$ schedls new backup -i
```

Creates a scheduled job. `NAME` and a backend (`--timer` or `--cron`) are
required unless `--interactive` is used.

Cron jobs are created for the current user's crontab. With `--system` (root
only) the job is created as an `/etc/cron.d/schedls-NAME` drop-in and runs as
`root` unless `--run-as USER` is given. System cron names may contain letters,
digits, `_` and `-` only, because cron ignores `cron.d` file names containing a
period. `--run-as` is valid only together with `--cron --system`.

The command to run is everything after `--`:

```console
$ schedls new backup --timer --daily 02:00 -- /usr/local/bin/backup "/srv/my data"
```

Arguments after `--` are passed literally as an argv; `schedls` does not
interpret `|`, `>`, `$()` or `$VAR`. Use `--shell SCRIPT` instead when you
explicitly want `/bin/sh -c`:

```console
$ schedls new rotate --timer --daily 03:00 --shell 'find /tmp -mtime +7 -delete'
```

`--shell` and a command after `--` are mutually exclusive.

### `edit NAME`

```console
$ schedls edit backup --daily 03:00
$ schedls edit backup --jitter 5min --no-persistent
$ schedls edit backup --command -- /usr/local/bin/backup /srv/data
$ schedls edit backup -i
```

Changes a job created by `schedls`. Only the options you pass are changed;
everything else keeps its current value.

- To replace the command, pass `--command` before the new command after `--`.
  Passing `--` without `--command` is an error, so the command can never be
  changed by accident.
- `--no-persistent` clears `Persistent=` on a systemd timer.
- Editing is supported for systemd timers only. Editing cron jobs is not yet
  supported and reports a usage error.

### `rm NAME`

```console
$ schedls rm backup
$ schedls rm backup --dry-run
```

Removes a schedls-managed job and its files. Unmanaged jobs cannot be removed.
For a system cron job this removes the `/etc/cron.d/schedls-NAME` drop-in and
refuses to touch anything whose markers no longer match.

### `enable NAME` / `disable NAME`

```console
$ schedls enable backup
$ schedls disable backup
```

Enable or disable a systemd timer. Cron has no portable enabled/disabled
concept, so these commands explain that limitation and exit without changing
anything.

### `logs NAME`

```console
$ schedls logs backup
$ schedls logs backup --lines 200
$ schedls logs backup --since '2026-09-01' --lines 500
```

Shows recent journal output for a systemd timer's service unit. The command is
never re-executed; this only reads the journal through `journalctl --no-pager`.

| Option         | Default | Meaning                                                                 |
|----------------|---------|-------------------------------------------------------------------------|
| `--lines N`    | `50`    | number of recent journal entries to show (`journalctl --lines=N`); `N` must be between `1` and `1000000` |
| `--since TIME` | none    | only entries since `TIME`, passed verbatim to `journalctl --since`      |

`TIME` accepts anything `journalctl --since` accepts, such as
`'2026-09-01'`, `'2 hours ago'` or `'2026-09-01 10:00:00'`. `--since` bounds the
range and `--lines` caps how many entries within it are shown, so raise
`--lines` to see more of a longer history.

Journal output is untrusted program output. When stdout is a terminal, control
characters are escaped (`\x1b` and friends) so a log line cannot inject terminal
escapes; when output is redirected to a file or pipe the bytes are passed
through unchanged. With `--json`, the command emits a document instead of raw
text:

```json
{
  "schema_version": 1,
  "name": "backup",
  "backend": "systemd",
  "unit": "schedls-backup.service",
  "content": "..."
}
```

Cron jobs have no portable per-job log interface. For a cron job, `schedls logs`
explains this (cron may mail output, redirect it, or write to the system log)
and exits `0`.

### `calendar EXPR`

```console
$ schedls calendar 'Mon..Fri 02:30'
$ schedls calendar --next 10 --utc daily
```

Validates a systemd `OnCalendar` expression with `systemd-analyze calendar` and
prints its normalized form and upcoming occurrences. Nothing is created.

| Option      | Default | Meaning                             |
|-------------|---------|-------------------------------------|
| `--next N`  | `5`     | how many upcoming occurrences to show |

Requires `systemd-analyze`. Without it the command reports a missing dependency.

### `doctor`

```console
$ schedls doctor
```

Reports which native facilities are usable on this host: systemd manager
availability, `systemd-analyze`, user lingering, `crontab` availability, the
detected cron implementation, whether the current user may use cron, and
whether the local `crontab` supports syntax validation. It only inspects; it
never changes anything.

## Schedule options

`new` and `edit` accept the same schedule options. Convenience flags compile to
native syntax; see the per-backend tables in [systemd.md](systemd.md#calendar-expressions)
and [cron.md](cron.md#schedule-shortcuts).

| Option                  | Backend        | Example                          |
|-------------------------|----------------|----------------------------------|
| `--calendar EXPR`       | systemd only   | `--calendar 'Mon..Fri 02:30'`    |
| `--daily TIME`          | both           | `--daily 02:00`                  |
| `--weekdays TIME`       | both           | `--weekdays 08:30`               |
| `--weekly DAY TIME`     | both           | `--weekly sun 04:00`             |
| `--monthly DAY TIME`    | both           | `--monthly 15 06:00`             |
| `--cron-expr EXPR`      | cron only      | `--cron-expr '0 4 * * 0'`        |
| `--persistent`          | systemd only   | run events missed while powered off |
| `--jitter DURATION`     | systemd only   | `--jitter 5min`                  |
| `--accuracy DURATION`   | systemd only   | `--accuracy 1min`                |
| `--working-directory PATH` | systemd only | `--working-directory /srv/data`  |
| `--env KEY=VALUE`       | systemd only   | `--env TZ=UTC` (repeatable)      |
| `--shell SCRIPT`        | both           | `--shell 'echo hi \| tee /tmp/log'` |

Notes:

- `--calendar` may be repeated; systemd supports multiple `OnCalendar=` entries.
- Do not combine `--calendar`/`--cron-expr` with the convenience flags; `schedls`
  rejects mixing them rather than guessing which you meant.
- Backend-specific options are rejected for the wrong backend (for example
  `--cron-expr` with `--timer`, or `--persistent` with `--cron`) instead of
  being silently ignored.
- Cron accepts a single schedule.
- Cron environment variables are not supported yet; passing `--env` with
  `--cron` is rejected rather than silently ignored.
- Cron jobs are always created for the current user. `--system` selects system
  scope for systemd timers and is rejected for cron.
- `new --timer` defaults to user scope. System scope requires appropriate
  privileges; `schedls` never runs `sudo` for you.

## Mutation options

These options apply to `new`, `edit`, `rm`, `enable` and `disable`:

| Option         | Meaning                                                                 |
|----------------|-------------------------------------------------------------------------|
| `--dry-run`    | print the plan and exit without changing anything                       |
| `--yes`        | do not ask for confirmation (for scripts)                               |
| `--show-files` | include the rendered file contents in the preview                       |
| `-i, --interactive` | collect missing fields with guided prompts (see below)             |

Every mutation prints a preview of exactly what will be written (paths, and with
`--show-files` the contents) and asks for confirmation before applying. Without
a terminal, confirmation fails unless `--yes` is passed.

## Interactive mode

`new` and `edit` accept `-i`/`--interactive` to fill missing fields with guided,
line-based prompts. Provided flags act as pre-filled defaults and are not
re-asked.

```console
$ schedls new backup -i
$ schedls edit backup -i
```

- Prompts use stdin only; no external editor or pager is ever launched.
- `new` asks for the scope (user or system) unless `--user`/`--system` was
  given. Choosing system for a cron job also asks which user it should run as
  (`--run-as`, default `root`).
- A terminal is required. With piped or redirected input, `schedls` exits `3`
  and asks you to pass flags instead.
- Interactive mode cannot be combined with `--json` (exit `2`).
- Collected values still go through the normal preview and confirmation, so
  nothing is written without showing you the resolved schedule and files first.

## Exit codes

| Code | Meaning                                                      |
|------|--------------------------------------------------------------|
| `0`  | success                                                      |
| `1`  | operational failure (helper command, filesystem, discovery)  |
| `2`  | invalid command-line input or invalid schedule               |
| `3`  | safety refusal / conflict (unmanaged object, concurrency, no TTY for confirmation) |

## Environment

| Variable   | Effect                                             |
|------------|----------------------------------------------------|
| `NO_COLOR` | when set, disables color even with `--color auto`  |

Helper commands run with a controlled environment (`LC_ALL=C`,
`SYSTEMD_COLORS=0`, `SYSTEMD_PAGER=cat`, `PAGER=cat`) and are never run through
a shell.
