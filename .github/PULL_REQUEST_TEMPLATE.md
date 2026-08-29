<!--
Before opening this PR, make sure the "Full verification before committing" steps
in CONTRIBUTING.md pass locally. Fill in the sections below.
-->

## Summary

<!-- What changed, in one or two sentences. -->

## Why

<!-- Why this change is needed. Reference the related issue if there is one,
     for example: Closes #123 -->

## Changes

<!-- The main changes, as a list. -->

-

## How this was tested

<!-- How you verified the change. Note any tests you added or modified. -->

## Checklist

- [ ] `black`, `isort`, `ruff`, and `mypy` pass.
- [ ] `pytest -m "not browser"` passes and coverage stays at or above the 85% floor.
- [ ] If the UI changed, `pytest -m browser` passes.
- [ ] If the IaC changed, `cfn-lint infra/*.yaml` passes.
- [ ] If shell scripts changed, `shellcheck scripts/*.sh` passes.
- [ ] If the API schema changed, the spec was regenerated with `scripts/export_openapi.py`.
- [ ] If runtime dependencies changed, the lock file was regenerated with `lock_requirements.sh`.
- [ ] If the change is user visible, an entry was added to `CHANGELOG.md`.
- [ ] No credentials or tokens are left in logs, code, or documentation.
