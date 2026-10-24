# Contributing

This repository is public. Two rules keep the lab it is tested in private.

## Secrets and device credentials

A test that needs live credentials, such as a device's secret code and MAC address or a Home
Assistant token, reads them from a git-ignored file with mode `600` on the host that runs the
test. Never put them in the repository. The field-test harness's file is described in
`docs/field-test-ha.md` (Credentials).

Unit tests use made-up values only, never a real device's:

- for MAC addresses, prefer the RFC 7042 documentation range (`00:00:5E:00:53:xx`) or locally
  administered ones (`02:…`);
- made-up secrets and names.

## No lab identifiers

None of these go in tests, fixtures, docs, commit messages, or PR and issue text:

- the lab's MAC addresses, or even their vendor prefixes;
- ESPHome and proxy hostnames, and host names;
- area, room and device names;
- config entry ids.

Field-test reports name things by neutral labels (`H60A6 #2`, `proxy A`, `area 3`). Check a report
before attaching it anyway.

## The gate

`scripts/check.sh` runs what CI runs: ruff, mypy, and pytest with the coverage floor. Run it before
every PR. The integration's dependency rules are in `CLAUDE.md`.
