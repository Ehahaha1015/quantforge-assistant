# Project workflow

The active product is a stock screenshot analysis and report assistant, with an
isolated offline showcase. Preserve its existing task, authorization and budget
boundaries. Offline fixtures do not establish model or forecast accuracy.

## Standing delivery instruction

The project owner explicitly requested on 2026-10-18 that every completed update
be pushed to https://github.com/Ehahaha1015/quantforge-assistant after appropriate
checks, diff review and commits. Use main by default and preserve remote changes.
Routine verified commits and pushes are already authorized; do not ask for the
same confirmation again unless the owner changes this instruction.

Keep the complete project layout when publishing from a workspace with separate
component repositories. Use fast-forward updates; never replace main with a
component-only tree or force-push. Exclude credentials, account configuration,
private original images, runtime databases, budget journals and unrelated private
history. Verify the remote commit. If a push is blocked or fails, explain the
remaining step and do not describe a local-only commit as synchronized.

CI runs the full Platform suite including DPAPI on Windows. Linux additionally
checks the active screenshot, personal assistant, QQ and worker paths. Preserve
the full Windows test coverage and all existing assertions.
