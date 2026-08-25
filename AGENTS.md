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

# PhotoPainter Rev2.0 hardware handoff

Before changing, compiling, flashing, or diagnosing Waveshare
ESP32-S3-PhotoPainter Rev2.0 firmware, read
[`docs/devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md`](docs/devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md)
and the current
[`docs/devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md`](docs/devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md).

The verified Rev2.0 EPD rail is TG28 **ALDO4**, not ALDO3 or an AXP2101
assumption. Preserve GPIO0 BOOT, GPIO5 PWR, GPIO21 TG28 IRQ, the narrow PMIC
write allowlist, the recoverable full-flash backup boundary, and the distinction
between Hosted CI and physical panel acceptance. Do not repeat destructive or
broad PMIC experiments when the handoff already contains an A/B result.
