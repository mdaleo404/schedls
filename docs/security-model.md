# Security model

This document describes what `schedls` trusts, what it does not, and how the
main threats are mitigated. It complements [SECURITY.md](../SECURITY.md).

## Trust model

- **Native schedulers are authoritative.** systemd and cron own execution.
  `schedls` only reads and writes their configuration.
- **Discovered configuration is data.** Commands and schedules found on the
  host are parsed and displayed, never executed.
- **Unmanaged objects are protected.** A timer or cron entry not created by
  `schedls` cannot be modified or removed by default.
- **The user chooses privilege.** `schedls` never escalates on the user's
  behalf.

## Threats and mitigations

### Malicious schedule names

Names must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Path separators, traversal,
whitespace and shell metacharacters are rejected. Names are never interpolated
into shell strings.

### Malicious command arguments

Arguments are kept as an argv internally. systemd units are rendered with a
dedicated serializer that always quotes arguments and escapes backslash,
double quotes, literal `$` and `%`; arguments containing NUL or newline are
rejected. cron commands are serialized with shell quoting and then have every
`%` escaped for cron's own parser. Calendar expressions are validated with
`systemd-analyze` and must not contain control characters or begin with `-`;
timer durations (`--jitter`, `--accuracy`) are validated with
`systemd-analyze timespan`. If a safe representation cannot be guaranteed,
creation is refused.

### Symlink attacks during privileged writes

Before replacing a file, `schedls` uses `lstat`, rejects symlinks and
non-regular files, and verifies ownership. Destination directories must be
real directories owned by the expected user (`root` when running as root) and
must not be group- or other-writable. Temporary files are created in the same
directory and atomically renamed.

### PATH hijacking

Critical helpers (`systemctl`, `systemd-analyze`, `journalctl`, `crontab`,
`loginctl`) are resolved once with a controlled lookup. A helper is rejected
unless the executable **and** its realpath target are regular files, are owned
by root (when `schedls` runs as root) or by root or the current user, and live
in a directory that is not group- or other-writable. Helper processes run with
a fixed `PATH` rather than the caller's.

### Path traversal through discovered unit names

A `Unit=` value read from a timer file is treated as untrusted data. Mutation
never uses it to build a filesystem path: unit names are always derived from
the validated job name (`schedls-<name>.timer` / `.service`). Generated unit
names are re-validated before they are written to a temporary directory, so a
crafted unit cannot redirect writes or removals outside the trusted unit
directory.

### Untrusted output

`journalctl` output is program output, not configuration. When stdout is a
terminal, control characters are escaped before display so a log line cannot
inject terminal escape sequences; redirected output is passed through
unchanged. Machine-readable `--json` output escapes control characters as part
of JSON encoding.

### Environment injection and pagers

Helper commands run with a controlled environment (`LC_ALL=C`,
`SYSTEMD_COLORS=0`, `SYSTEMD_PAGER=cat`, `PAGER=cat`) and never with a shell.
Pagers and editors are never launched while `schedls` is running. Environment
values passed with `--env` are written to unit files with systemd quoting and
are world-readable like any other unit; do not put secrets in them.

### TOCTOU races

Plans snapshot existing content. At apply time the current content is compared
against the snapshot; a mismatch aborts the operation. Privileged critical
sections are kept small and use file descriptors / atomic rename where
practical.

### Corrupt or malicious existing configuration

Configuration is treated as opaque data. Malformed `schedls` cron markers cause
mutation to fail closed rather than guess. Unknown crontab lines are preserved
byte-for-byte. A systemd unit whose name starts with `schedls-` is only treated
as managed when its file carries the `# Managed by schedls` and `# Name:`
markers, so a same-named foreign unit is not modified.

### Partial operations

systemd creation writes both units, reloads the manager, and enables the timer;
if a later step fails, or the operation is interrupted, the previous state is
restored and a partially enabled timer is disabled again. Cron installs are
validated first and restored from the previous crontab text on failure.

### Supply chain

Runtime dependencies are zero. Development dependencies are locked and audited
in CI.

## Availability, privacy

`0.1.0` requires `systemd-analyze` to create or validate calendar expressions
and refuses the operation if it is unavailable. `schedls` makes no network
requests and collects no telemetry.
