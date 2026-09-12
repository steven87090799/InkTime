# Start here: scoped reading

Before editing, read [`docs/AI_NAVIGATION.md`](docs/AI_NAVIGATION.md).
Select the matching task row and read only its entry points and relevant sections.
Do not preload the full README, HTML manual, documentation tree, or repository.
Expand to callers, contracts, and tests when evidence requires it.

## AI read boundary and context budget

Use [`docs/AI_CONTEXT_INDEX.json`](docs/AI_CONTEXT_INDEX.json) as the
machine-readable route and read-policy index. In the first pass, read at most
4 files and 800 source/documentation lines. Expand only when a caller,
contract, test, or safety boundary is required by evidence; record the reason
when the budget is exceeded. A full-repository audit requires an explicit user
request. Use `python scripts/ci/ai_context.py <task-id>` to print a bounded
route summary and symbol/heading locations before opening a large file.

Do not open secrets, runtime data, generated output, or binary/media assets by
default: `.env*`, `*.db`, `*.sqlite*`, `*.lock`, `data/`, `output/`, `photos/`,
`simulation_photos/`, `logs/`, `.git/`, `docs/archive/`, fonts, images, PDFs,
ZIPs, and firmware binaries are excluded unless the task names an exact path
and requires it. `.gitignore` is not an AI read policy. Use targeted `rg` and
bounded `sed` reads instead of whole-file dumps. The PhotoPainter hardware
handoff documents named below remain a safety exception and must be read in
full before hardware work.

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

## Authorized unused-audio power change (2026-09-06)

The user explicitly requested disabling unused microphone/audio power. The
current Rev2.0 schematic confirms **ALDO3 (UP1 pin 16) -> Audio_VCC**, while
ALDO2 (pin 19) is unconnected; older handoff text naming ALDO2 was incorrect.
The additional narrow allowlist is clearing **REG90[2] only**, at startup after
PA LOW, with full-byte readback. Preserve all other bits and retain the existing
ALDO4 EPD contract. Do not expand this authorization to other PMIC rails, sleep,
IRQ, charging, or shutdown registers. The SD card is directly on VCC3V3 and has
no independent power gate. See the current PhotoPainter guide for source links,
software evidence, and the outstanding physical acceptance checks.
