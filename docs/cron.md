# cron backend

`schedls` reads every cron source visible to it:

- the **current user's crontab**, through the `crontab` utility;
- **system crontabs**: `/etc/crontab`, `/etc/cron.d/*`;
- **periodic directories**: `/etc/cron.{hourly,daily,weekly,monthly}/*`;
- **other users' crontabs** (`/var/spool/cron/crontabs/*` on Debian-style
  systems, `/var/spool/cron/*` elsewhere) when running as root.

Cron jobs are named after where they live: system entries are `<file>:<line>`,
periodic scripts are named after the script, spool entries are `<user>:<line>`,
and the current user's crontab entries are `cron-<line>`. Jobs created by
`schedls` are listed under their schedule name instead.

`schedls` writes either through the `crontab` utility (current user) or as an
`/etc/cron.d/schedls-<name>` drop-in (system, root only). It never writes to
`/var/spool/cron*` directly.

## Reading

The user crontab is read with `crontab -l` and parsed losslessly into lines:

- blank lines;
- comments;
- environment assignments (`KEY=VALUE`);
- five-field entries and `@` nicknames;
- `schedls:begin` / `schedls:end` managed-block markers.

System crontabs add a user field between the schedule and the command
(`min hour dom mon dow USER command`). Files in `/etc/cron.d` whose names
contain a period, and hidden files, are ignored because cron ignores them too.
Periodic directories only contribute executable, run-parts-style file names.

Every unrelated line is preserved exactly. `schedls` does not reformat, sort,
normalize, or rewrite crontab content it did not create.

## Run times

For five-field expressions and calendar nicknames, schedls calculates the next
cron occurrence in the host's local timezone for human-readable output.
`@reboot` has no wall-clock next run. Cron does not expose per-entry execution
history, so previous run times and results are shown as unavailable rather than
guessed. To keep the stable JSON document contract, cron `next_run` remains
`null` in `--json` output.

## Managed blocks

Jobs created by `schedls` in the current user's crontab live in a delimited
block:

```cron
# schedls:begin name=backup
0 2 * * * /usr/local/bin/backup /srv/data
# schedls:end name=backup
```

If markers are malformed or a `begin` has no matching `end`, mutations refuse
to proceed rather than guess.

Existing active blocks need no migration. A disabled managed job keeps the same
block and replaces its job line with a column-zero comment:

```cron
# schedls:begin name=backup
# schedls:disabled 0 2 * * * /usr/local/bin/backup /srv/data
# schedls:end name=backup
```

The literal `# schedls:disabled ` prefix is the disabled representation; the
unprefixed line is enabled. This keeps the job discoverable and reversible
without giving cron a new file-wide setting.

## Editing

`schedls edit` supports jobs in valid schedls-managed blocks. It updates only
the single cron job line in the block, leaving comments, environment
assignments, and all other cron entries unchanged. If a block has malformed
markers or does not contain exactly one job line, schedls refuses to edit it.

When only the schedule changes, schedls retains the existing cron command
without reverse-parsing or re-quoting shell syntax. Replacing a command uses
the normal safe command renderer.

When an environment is replaced or cleared, schedls preserves the existing raw
command field and replaces only its managed assignment prefix. A malformed or
non-canonical managed prefix produces a warning and makes mutation refuse
rather than guess at shell syntax.

For a system drop-in, editing requires root and re-checks the expected
`/etc/cron.d/schedls-<name>` path, ownership, trusted directory, and file
snapshot before atomically replacing the file. The existing `run-as` user is
preserved.

## System drop-ins

A system cron job is a file named after the schedule:

```cron
# Managed by schedls
# schedls:begin name=backup
0 2 * * * root /usr/local/bin/backup /srv/data
# schedls:end name=backup
```

Only `/etc/cron.d/schedls-<name>` files carrying a matching managed block are
treated as managed. A `schedls-` prefixed file whose markers are missing or
malformed is listed as unmanaged (`schedls-<name>:<line>`) and cannot be
removed. Creating system jobs requires root and a trusted `/etc/cron.d`
directory: not a symlink, owned by root, and not group- or other-writable.
Removal deletes only the drop-in file, after re-checking that it still matches
the snapshot and markers. Cron picks up `/etc/cron.d` changes by itself, so no
daemon reload is issued.

Use `--run-as USER` to run a system job as another account (default `root`).
The interactive wizard (`schedls new -i`) asks for the scope, and choosing the
system scope for a cron job then prompts for the run-as user.

## Per-job environment

`--env KEY=VALUE` creates execution-local POSIX shell assignments on a managed
job line. They are not file-wide crontab environment declarations, so they do
not affect unrelated jobs. A user job is rendered like this:

```cron
0 2 * * * TZ=UTC /usr/local/bin/backup
```

For a system job, the run-as account stays before the assignments:

```cron
0 2 * * * backup TZ=UTC /usr/local/bin/backup
```

`--env` may be repeated. On creation it supplies the complete environment. On
edit, omitting it preserves the current values; supplying one or more entries
replaces the complete set; and `--clear-env` explicitly removes every value.
`--env` and `--clear-env` are mutually exclusive. The interactive wizard offers
the same choices, including an empty replacement to clear the environment.

Environment values are shell-quoted and percent-escaped with the command field.
They are scheduler configuration, not secret storage: current-user crontabs and
system cron drop-ins may be readable by other users.

## Command rendering

Cron executes command text through a shell. `schedls` keeps an argv internally
and serializes it safely:

1. each argument is quoted for `/bin/sh` (`shlex.quote`);
2. every `%` is escaped as `\%`, because cron implementations such as Cronie
   treat an unescaped `%` as a newline.

Arguments containing NUL or newline are rejected, because no safe single-line
representation exists.

Raw shell syntax is available explicitly with `--shell`. `schedls` never infers
shell mode from characters such as `|`, `>`, `&&` or `$()`.

For cron `--shell`, a managed environment is an ordinary leading POSIX
assignment. In a pipeline such as `TZ=UTC producer | consumer`, it applies to
the first command only; schedls does not add a wrapper to widen the assignment's
scope. Export a value in the script when later pipeline commands need it.

## Schedule shortcuts

| Flag              | Example               | Compiles to       |
|-------------------|-----------------------|-------------------|
| `--daily TIME`    | `--daily 02:00`       | `0 2 * * *`       |
| `--weekdays TIME` | `--weekdays 08:30`    | `30 8 * * 1-5`    |
| `--weekly D TIME` | `--weekly sun 04:00`  | `0 4 * * 0`       |
| `--monthly D TIME`| `--monthly 15 06:00`  | `0 6 15 * *`      |
| `--cron-expr EXPR`| `--cron-expr '0 4 * * 0'` | used verbatim |

## Installation and validation

When the local `crontab` supports syntax testing (`crontab -T`), a new user
crontab is validated before installation. The native `crontab` install remains
authoritative. The previous crontab text is retained and restored if
installation fails or the crontab changed since the plan was prepared.

System drop-ins are validated when they are rendered and written atomically;
`crontab -T` only understands user-format crontabs, so it is not used for them.
A file written by `schedls` is re-read and its managed block verified before
the command reports success.

Capabilities are feature-detected, never assumed:

```python
CronCapabilities(
    supports_validation=True,
    supports_user_selection=False,
    implementation="Cronie",
)
```

## Enable / disable

`schedls enable` and `schedls disable` support schedls-managed cron jobs using
the disabled-line representation above. They are idempotent and only add or
remove that prefix, preserving the saved schedule and command text exactly.
Unmanaged cron jobs are never changed.

Older schedls versions do not understand the disabled-line representation. They
will leave its job inactive because cron treats it as a comment, but cannot edit
that block correctly. Re-enable the job with a version that supports this
format before downgrading if you need to manage it with an older release.

## Logs

There is no portable per-job log interface for cron. `schedls logs` says so
rather than pretending otherwise. `--lines` and `--since` are accepted for
command-line compatibility but have no effect on cron jobs; see
[cli.md](cli.md#logs-name).

## Not yet supported

- token-level updates of `/etc/crontab` (read-only, shared file);
- anacron (`/etc/anacrontab`);
- removing or editing periodic directory scripts;
- modifying other users' spool crontabs (read-only, root only).
