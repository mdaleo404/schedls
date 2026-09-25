# systemd backend

`schedls` manages systemd **timers**. A timer is always paired with a
oneshot service that holds the command.

## Layout

| Scope  | Unit directory               | Manager              |
|--------|------------------------------|----------------------|
| user   | `~/.config/systemd/user`     | `systemctl --user`   |
| system | `/etc/systemd/system`        | `systemctl`          |

`new --timer` defaults to user scope. System scope must be requested
explicitly with `--system` and appropriate privileges; `schedls` never invokes
`sudo` for you.

## Created files

For a job named `backup`:

```ini
# ~/.config/systemd/user/schedls-backup.service
# Managed by schedls
# Name: backup

[Unit]
Description=schedls job backup

[Service]
Type=oneshot
ExecStart="/usr/local/bin/backup" "/srv/data"
```

```ini
# ~/.config/systemd/user/schedls-backup.timer
# Managed by schedls
# Name: backup

[Unit]
Description=schedls job backup (timer)

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
Unit=schedls-backup.service

[Install]
WantedBy=timers.target
```

The `schedls-` prefix and the header comments provide provenance without a
database.

## Argument handling

`ExecStart=` is not shell syntax. `schedls` serializes each argument with
systemd's own quoting rules and always quotes arguments, so a value such as
`$(...)`, `%i`, `$HOME` or an argument beginning with `-` is passed literally.
Use `--shell` to explicitly request `/bin/sh -c`.

Every rendered unit is validated with `systemd-analyze verify` before
installation.

## Calendar expressions

Convenience flags compile to native `OnCalendar=` values:

| Flag             | Example                    | Compiles to                        |
|------------------|----------------------------|------------------------------------|
| `--daily TIME`   | `--daily 02:00`            | `*-*-* 02:00:00`                   |
| `--weekdays TIME`| `--weekdays 08:30`         | `Mon..Fri *-*-* 08:30:00`          |
| `--weekly D TIME`| `--weekly sun 04:00`       | `Sun *-*-* 04:00:00`               |
| `--monthly D TIME`| `--monthly 1 06:00`       | `*-*-01 06:00:00`                  |
| `--calendar EXPR`| `--calendar 'Mon..Fri 02:30'` | used verbatim                   |

`--calendar` may be repeated; systemd supports multiple `OnCalendar=` entries.

## Discovery

Timers are listed with `systemctl list-unit-files` and `list-units`, then
properties are read with `systemctl show`. OnCalendar, Persistent and the
command are read from the unit fragments. The next elapse is taken from
`NextElapseUSecRealtime`; monotonic timers have no wall-clock next run and are
reported as unknown.

## Logs

`schedls logs NAME` queries the service unit's journal via `journalctl
--no-pager`. The job command is never re-executed.

Two options control how much is shown, both passed to `journalctl`:

| Option         | Default | Meaning                                                       |
|----------------|---------|---------------------------------------------------------------|
| `--lines N`    | `50`    | number of recent entries (`journalctl --lines=N`)             |
| `--since TIME` | none    | lower bound on entry time (`journalctl --since=TIME`)         |

`TIME` accepts any value `journalctl --since` accepts, for example
`'2026-09-01'`, `'2 hours ago'` or `'2026-09-01 10:00:00'`. `--since` bounds the
range and `--lines` caps how many entries within it are shown. See
[cli.md](cli.md#logs-name) for the full command reference.

## Lingering

User timers only run while the user's systemd manager is alive. `schedls`
warns when lingering is disabled but never enables it.
