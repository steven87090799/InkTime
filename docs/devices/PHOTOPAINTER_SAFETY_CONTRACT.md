# PhotoPainter Rev2.0 Hardware Safety Contract

Current safety rules, checked against the Rev2.0 guide and hardware handoff.
Read before PMIC/power/GPIO/boot/flash/storage-bus or physical panel work.
Server/API/schedule/playlist/manifest work alone does not require hardware history.

## Power and pin ownership
- TG28 at I2C `0x34`, not an AXP2101 register-map assumption. EPD_VCC is
  **ALDO4**: `REG95[4:0]=0x1C` (3.3 V), `REG90[3]=1`.
- **Never disable ALDO3 / Audio_VCC (`REG90[2]`)**. Unpowered audio codecs can
  clamp shared SDA/SCL and break the next TG28 read and EPD refresh. Keep ALDO3
  powered, PA GPIO7 LOW, I2S GPIO14–18 input/uninitialized. ALDO2 is unconnected.
  A future exception needs separate hardware isolation, schematic review and
  explicit cold-boot I2C/panel acceptance; a warm reset or build is insufficient.
- GPIO0 is BOOT: do not sample/drive for application behavior. GPIO5 is PWR /
  SYS_OUT: do not drive or repurpose as software sleep/wake. GPIO21 is open-drain
  TG28 IRQ: never drive LOW/HIGH. Preserve GPIO4 KEY1 and timer recovery wake.
- Shared I2C: SDA47/SCL48, 100 kHz. EPD: DC8/CS9/SCK10/MOSI11/RST12/BUSY13
  (BUSY active-low). Preserve bus ownership; LEDs are GPIO45/42, not GPIO5.

## Verified PMIC write allowlist
Only EPD voltage `REG95[4:0]` and enable `REG90[3]`, with read-modify-write,
preservation of every other bit and readback after each write. Do not clear
REG90[2]. Never disable ALDO4 before refresh/deep sleep; use panel controller
POWER_OFF only. Never push-pull SDA/SCL HIGH to recover a stuck bus. No broad register sweeps/writes, DCDC, other LDOs, IRQ, charging,
shutdown, fast-power-on or speculative rail experiments. On failed PMIC reads,
stop repeated rail/EPD manipulation; use the documented full board power-off
recovery. Repeated resets do not replace that sequence.

## Flash and storage recovery
Preserve a recoverable full 16 MiB flash backup before destructive operations;
keep it local because it may contain credentials. Never publish its contents or
include it in CI artifacts. Honor an explicit user backup waiver while retaining
a recoverable, scoped flash plan. Prepare exact board, partition layout, artifact,
address and recovery commands before flashing. App-only starts at `0x10000`;
never expand into a full erase or partition/NVS overwrite by assumption.
SD is on VCC3V3 with no independent power gate, using SPI CS38/SCK39/MISO40/MOSI41
separate from EPD. Enhanced runtime uses Internal FFat: `formatOnFail=false`;
format only after verifying the **entire** FAT partition is erased (`0xff`).
Mount failure with data must preserve the partition and report STORAGE_CORRUPT.

## Evidence and when to expand
Compile, flash digest, simulator, Hosted CI and ACK do not prove physical panel,
power-loss, cold-boot, storage or wake acceptance. Record these separately.
Read the relevant sections of the [current guide](WAVESHARE_PHOTOPAINTER_ZH_TW.md)
for exact flashing/recovery/storage steps; read the
[historical A/B handoff](PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md)
only for an exact historical diagnosis, measurement or official-firmware comparison.
Do not repeat destructive experiments already resolved there.
