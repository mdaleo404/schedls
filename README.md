[![Licence](https://img.shields.io/badge/GPL--3.0--or--later-orange?label=Licence)](https://git.sysmd.uk/mdaleo404/schedls/src/branch/main/LICENSE)
[![Gitea Release](https://img.shields.io/gitea/v/release/mdaleo404/schedls?gitea_url=https%3A%2F%2Fgit.sysmd.uk%2F&style=flat&color=orange&logo=gitea)](https://git.sysmd.uk/mdaleo404/schedls/releases)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-blue?logo=pre-commit&style=flat)](https://git.sysmd.uk/mdaleo404/schedls/src/branch/main/.pre-commit-config.yaml)

# schedls

<div align="center">
  <img src="https://git.sysmd.uk/mdaleo404/schedls/raw/branch/main/schedls.png" alt="schedls logo" width="256" />
</div>

Inspect and manage the things Linux runs later.

`schedls` is a CLI tool for inspecting and managing cron jobs and
systemd timers through one transparent interface. It is a management and
inspection layer over the operating system's native schedulers — it is not a
daemon, not a queue, and not a replacement scheduler.

```console
$ schedls
NAME              SCHEDULE             NEXT              BACKEND  SCOPE   STATUS
backup            daily at 02:00       tomorrow 02:00    systemd  user    waiting
cleanup           0 4 * * 0            —                 cron     user    active
logrotate         daily                —                 systemd  system  waiting
```

If you uninstall `schedls`, everything it created keeps working: a timer is an
ordinary systemd unit, and a cron job is an ordinary crontab entry.

## Install

```console
$ pipx install schedls
# or
$ pip install --user schedls
```

`python -m schedls` works too. Zero runtime dependencies; Python 3.11+.

## Examples

Discover everything visible to you:

```console
$ schedls
$ schedls --system
$ schedls --backend cron
```

Explore a native calendar expression without creating anything:

```console
$ schedls calendar 'Mon..Fri 02:30'
$ schedls calendar --next 10 --utc daily
```

Create a user systemd timer:

```console
$ schedls new backup --timer --daily 02:00 --persistent -- /usr/local/bin/backup /srv/data
```

Create a cron job:

```console
$ schedls new cleanup --cron --cron-expr '0 4 * * 0' -- /usr/local/bin/cleanup
```

Prefer to be guided? Add `-i`/`--interactive` to `new` or `edit` and any
omitted field is prompted for. Flags you do pass become pre-filled defaults:

```console
$ schedls new backup -i
$ schedls edit backup -i
```

Interactive mode is line-based (no external editor or pager), requires a
terminal, and cannot be combined with `--json`. The collected values are shown
in the usual preview and still require confirmation before anything is written.

Inspect, change, disable, remove:

```console
$ schedls show backup
$ schedls edit backup --daily 03:00
$ schedls disable backup
$ schedls rm backup
$ schedls logs backup
```

Check what this host can do:

```console
$ schedls doctor
```

Every mutating command supports `--dry-run`, and `--yes` for scripts. Machine
output is available with `--json`.

## Design promises

`schedls` does not:

- run a background daemon;
- listen on a network port;
- make network requests;
- automatically invoke `sudo`;
- execute discovered scheduled commands;
- open an editor or pager while managing jobs;
- use a shell internally for helper commands;
- modify unmanaged schedules by default.

See [SECURITY.md](SECURITY.md) and [docs/security-model.md](docs/security-model.md).

## Documentation

- [docs/cli.md](docs/cli.md) — every command, option and exit code
- [docs/architecture.md](docs/architecture.md)
- [docs/security-model.md](docs/security-model.md)
- [docs/systemd.md](docs/systemd.md)
- [docs/cron.md](docs/cron.md)

## Development

```console
$ poetry install
$ poetry run pre-commit install      # check hooks on every commit
$ poetry run pre-commit run --all-files
$ poetry run pytest
$ poetry run mypy
```

Formatting and linting use [ruff](https://docs.astral.sh/ruff/); security
scanning uses [Bandit](https://bandit.readthedocs.io/); both run through
[pre-commit](https://pre-commit.com/) alongside trailing-whitespace, end-of-file,
YAML and TOML checks. CI runs the same hooks on every push and pull request,
followed by a strict type check, the test matrix (Python 3.11–3.14) and a package
build. A scheduled workflow builds an SBOM and scans it with Grype.
