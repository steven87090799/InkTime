# Hosted CI and delivery policy

GitHub Actions is the authoritative test, build, security-scan, benchmark,
firmware-compile, and hosted-runtime environment for this repository.

For ordinary coding work, do not run local `pytest`, `npm test`, Docker build or
compose smoke, Playwright, Arduino/PlatformIO/firmware compilation, benchmark,
paid-provider call, or runtime-soak commands. Use static source inspection and
`git diff --check` locally, then rely on the routed hosted checks.

After pushing a branch, inspect the resulting GitHub Actions runs once. Do not
use `gh run watch`, polling loops, `sleep`-based waits, reruns, or manual
dispatches to manufacture a green result. If a run is queued or in progress,
report `CI_PENDING` and hand the task back to the user.

Keep pull requests Draft until a human explicitly decides otherwise. Never
merge, mark ready, enable auto-merge, force-push, reset, or clean a worktree as
part of routine delivery.
