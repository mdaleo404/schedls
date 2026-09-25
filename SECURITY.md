# Security policy

`schedls` writes scheduler configuration and, when run as root, writes files in
privileged locations. Security is therefore a primary design concern.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the maintainer listed in
`pyproject.toml`. Do not open a public issue for a security-sensitive report.

## Promises

`schedls` does not:

- run a background daemon;
- listen on a network port;
- make network requests;
- automatically invoke `sudo`, `su`, `pkexec`, or any other privilege
  escalation mechanism;
- execute discovered scheduled commands;
- open an editor or pager while managing jobs;
- use a shell internally for helper commands (`shell=True` is forbidden);
- modify unmanaged schedules by default.

## Security model

See [docs/security-model.md](docs/security-model.md) for the threat model and
the mitigations implemented in this repository.

## Release checklist

Before every release:

```text
[ ] No new shell=True usage
[ ] No automatic privilege escalation
[ ] No new implicit command execution
[ ] No pager/editor invocation under privilege
[ ] Unit serialization tests pass
[ ] Cron serialization tests pass
[ ] Symlink tests pass
[ ] Atomic-write tests pass
[ ] Trusted-directory ownership/mode tests pass
[ ] Helper resolution ownership tests pass
[ ] Unit-name path traversal tests pass
[ ] Log sanitization tests pass
[ ] Dependency audit passes
[ ] JSON schema changes reviewed
[ ] New mutating operations support --dry-run
[ ] New mutating operations have clear confirmation behavior
[ ] Documentation describes new filesystem/state changes
```
