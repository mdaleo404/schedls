# cron backend

`schedls` reads and edits the **current user's crontab** through the `crontab`
utility. It never writes to `/var/spool/cron*` directly.

## Reading

The crontab is read with `crontab -l` and parsed losslessly into lines:

- blank lines;
- comments;
- environment assignments (`KEY=VALUE`);
- five-field entries and `@` nicknames;
- `schedls:begin` / `schedls:end` managed-block markers.

Every unrelated line is preserved exactly. `schedls` does not reformat, sort,
normalize, or rewrite crontab content it did not create.

## Managed blocks

Jobs created by `schedls` live in a delimited block:

```cron
# schedls:begin name=backup
0 2 * * * /usr/local/bin/backup /srv/data
# schedls:end name=backup
```

If markers are malformed or a `begin` has no matching `end`, mutations refuse
to proceed rather than guess.

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

## Schedule shortcuts

| Flag              | Example               | Compiles to       |
|-------------------|-----------------------|-------------------|
| `--daily TIME`    | `--daily 02:00`       | `0 2 * * *`       |
| `--weekdays TIME` | `--weekdays 08:30`    | `30 8 * * 1-5`    |
| `--weekly D TIME` | `--weekly sun 04:00`  | `0 4 * * 0`       |
| `--monthly D TIME`| `--monthly 15 06:00`  | `0 6 15 * *`      |
| `--cron-expr EXPR`| `--cron-expr '0 4 * * 0'` | used verbatim |

## Installation and validation

When the local `crontab` supports syntax testing (`crontab -T`), the new
crontab is validated before installation. The native `crontab` install remains
authoritative. The previous crontab text is retained and restored if
installation fails or the crontab changed since the plan was prepared.

Capabilities are feature-detected, never assumed:

```python
CronCapabilities(
    supports_validation=True,
    supports_user_selection=False,
    implementation="Cronie",
)
```

## Enable / disable

Cron has no universal native enabled/disabled concept. `schedls enable` and
`schedls disable` support systemd only and explain this limitation for cron.

## Logs

There is no portable per-job log interface for cron. `schedls logs` says so
rather than pretending otherwise. `--lines` and `--since` are accepted for
command-line compatibility but have no effect on cron jobs; see
[cli.md](cli.md#logs-name).

## Not yet supported

- system cron (`/etc/crontab`, `/etc/cron.d`, periodic directories);
- editing existing cron jobs;
- cross-user crontabs.
