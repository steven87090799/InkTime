# InkTime · E-Ink Memory Frame

> Source baseline: 2026-08-31, `48d2b8d`. Migration 52, AI Schema v3 and ESP32 2.8.6 are separate version contracts. See the [current-state reference](docs/reference/CURRENT_STATE_ZH_TW.md).

> For complete Web/Worker/Scheduler setup, device queues, resilience features, and the document map, also see the [Chinese README](README.md), the [documentation portal](USER_MANUAL.html), and [docs/README.md](docs/README.md).

[中文](README.md) | **English**

<p align="left">
  <img src="esp32/InkTime.jpeg" width="80%">
</p>

InkTime is an e-ink photo frame project that brings forgotten memories back from your photo library.

It does not show random photos, and it is not a simple chronological slideshow. Instead, it:

- Uses local metadata and quality features by default, with optional AI analysis
- Scores photos by "memory value" and visual quality
- Uses existing or local date/location captions; AI captions require an explicitly enabled provider
- Picks the most meaningful photo from "on this day" every day
- Pushes it to an ESP32-powered e-ink display

---
## Project Structure

InkTime has three main parts:

1. **Photo analysis (Python)**  
   Scan photo library -> compute local features -> optionally call an enabled vision provider -> store results in SQLite

2. **Image rendering (Python)**  
   Select high-scoring "on this day" photos from the database -> render `.bin` files that the ESP32 can display directly

3. **Download and display (ESP32)**  
   The ESP32 periodically downloads the `.bin` file from the server -> refreshes the e-ink screen -> enters deep sleep until the next wake-up

---
## Setup

### 1. Python

Python 3.10+ is required; the production image uses Python 3.12.

Using a virtual environment is recommended:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Image metadata

The current scanner reads EXIF/GPS with Pillow and HEIF support. ExifTool is not required and is not invoked by the production pipeline.

### 3. Configure the runtime

For local Docker development, copy `.env.local.example` to `.env`, set the actual paths and LAN URL, then use both `docker-compose.yml` and `docker-compose.dev.yml`. The development override exposes the Web port to the LAN by default; use a loopback bind for local-only access.

Native Python processes do not load the Compose `.env` automatically. Export `INKTIME_DATA_DIR`, `INKTIME_PHOTO_DIR` and other [runtime settings](docs/architecture/RUNTIME_CONFIGURATION.md) consistently in each process. Create the first administrator at `/setup`. API keys belong in the encrypted Web settings flow, not source files or deployment examples.

## Analyze Photos

New installations default to `analysis.execution_mode=local_only`; scanning, local selection and rendering do not require an AI provider. To run ordinary AI jobs, an administrator must explicitly select `automatic_ai` in `/settings`, configure a provider, its model and pricing, then create a small `single` job in `/jobs`. `local_with_manual_ai` permits explicit manual AI operations only.

The current pipeline sends one Vision image request per analysis plan, with at most one text-only JSON repair. Old two-stage strategy names normalize to `single`. Results include descriptions, photo types, scores and captions; the source schema is v3 and retains older results for reading.

`analyze_photos.py` is a compatibility CLI that creates persisted jobs and executes one Worker iteration. It is not the retired standalone analyzer; remaining work requires the Worker service. A completed local job, prefilter result, inherited analysis or cache hit does not prove a new provider call. Check [Activity and AI Trace](docs/guides/ACTIVITY_AI_TRACE_ZH_TW.md) together with usage and timestamps.

## Render the Daily "On This Day" Photo for ESP32

Use the Rendering page to preview and publish a release. The Scheduler and Worker use the same Modern renderer and release flow for daily updates.

## Start the ESP32 Download Server and Web UI

For native development, run these in separate terminals with the same environment and paths:

```bash
python3 server.py
python3 -m inktime.app.workers.runner
python3 -m inktime.app.workers.scheduler
```

Open `/setup` first, then `/photos`, `/rendering`, `/simulator`, `/activity` or `/ai/traces`. Production uses Gunicorn (`gunicorn --config gunicorn.conf.py server:app`) for Web, plus the Worker and Scheduler processes; Flask's development server is not a production service.

## Server Deployment and Scheduled Task Example (optional)

Production NAS deployment uses published GHCR images and `scripts/update_nas.sh`, not a local source build. Follow the [NAS deployment guide](docs/operations/NAS_TAG_DEPLOYMENT_ZH_TW.md) to prepare `.env.nas`, real canonical paths, UID/GID 10001 permissions, transport settings and a published version tag.

```bash
# Replace vX.Y.Z with an already published release.
sudo ./scripts/update_nas.sh --initialize vX.Y.Z
# Subsequent updates omit --initialize.
```

The updater checks the deployment marker, lock, contract and recovery point before recreating all three services without a build. HTTPS and secure cookies are the production default. Trusted-LAN HTTP is an explicit degraded mode and must not be exposed to the Internet. Configure daily schedules in the Web console; no legacy render cron is required.

For development validation, follow [AGENTS.md](AGENTS.md) and [CI policy](docs/CI_POLICY.md): tests, builds, browser suites and firmware compilation run in Hosted CI.

---

# ESP32 E-Ink Hardware

## Hardware and Pins

The custom PCB details below apply to the generic board profile, not the PhotoPainter pin map or button behavior.

#### MCU

This project uses the Espressif ESP32-S3-N8R8 module.

Use a supported compile-time board profile. GPIO, flash partitions, PSRAM and panel drivers must match the actual board; arbitrary ESP32 boards are not interchangeable.

#### Display

The current server provides three 480×800 wire profiles: safe four-color, GDEP073E01 six-color and GDEY073D46 seven-color. The matching compile-time driver and physical adapter are described in the ESP32 guide; PhotoPainter uses its dedicated board adapter.

Adding another size or model requires matching server payload, firmware driver, memory and hardware validation; changing only the display constructor is insufficient.

#### E-Ink Adapter Board

This project uses the 49-pin seven-color EPD adapter board made by the Bilibili creator "记得带马扎".

Connector pin count alone does not prove compatibility. Match the controller, voltage, pinout and selected panel profile before connecting hardware.

#### Pin Definitions

The e-ink display communicates over SPI. The default pins are:

- `PIN_EPD_BUSY = 14`
- `PIN_EPD_RST  = 13`
- `PIN_EPD_DC   = 12`
- `PIN_EPD_CS   = 11`
- `PIN_EPD_SCLK = 10`
- `PIN_EPD_DIN  = 9`

### PCB Assembly

The schematic, BOM, and PCB fabrication files are in the ```esp32/pcb``` folder.

H1-H6 in the schematic are test pads and do not need real components soldered:

- H1: UART serial
- H2: USB
- H3: BOOT pin. Short this pin to GND before powering on when flashing firmware.
- H4: Connects to the EPD adapter board
- H5: 3.7V battery pads
- H6: 5V input test pads

UART flashing is recommended. R2, R3, C5, and C6 are used for USB; leave them unpopulated if USB is not needed.

SW1: RESET button. Pressing it restarts the device and downloads/displays the image once. It can also wake the device from long deep sleep.  
SW2: Wi-Fi reset button. Hold SW2 and press SW1; after restart, the ESP32 clears NVS so Wi-Fi can be configured again.  
SW3 / SW4: Reserved GPIOs for possible future features. Leave them unpopulated if not needed.

Example PCB:

<p align="left">
  <img src="esp32/pcb/pcb.jpeg" width="80%">
</p>

## Build and Flash

The shared 7C/PhotoPainter source currently declares firmware **2.8.6**. Use the exact Hosted CI profile, pinned dependencies and repository-owned partition table described in the [ESP32 guide](docs/devices/ESP32_GUIDE_ZH_TW.md).

PhotoPainter Rev2.0 requires 16 MiB flash, 8 MiB OPI PSRAM, TG28 ALDO4 power handling and its own GPIO map. Read the [hardware handoff](docs/devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md) before flashing. Preserve a full local flash backup; an app-only binary belongs at `0x10000`, never `0x0`. GPIO0 remains BOOT and GPIO5 remains the factory PWR button.

The 13.3-inch beta sketch uses a retired download protocol and is not integrated with the current Web release profiles.

### Custom Fonts (optional)

InkTime includes two offline Traditional Chinese choices in the Rendering page: Iansui for a handwriting style and LXGW WenKai TC for a literary style. Administrators can preview and switch between them, or upload a TTF/OTF/TTC file up to 64 MiB. Formal rendering checks every caption character and fails explicitly instead of silently falling back to Pillow's default font.

## First-Time Configuration

On startup, the device tries to read saved Wi-Fi credentials from NVS. If credentials are missing or Wi-Fi connection fails, it automatically enters AP configuration mode:

- The device starts an AP hotspot: `InkTime-xxxx`
- The current ESP32-S3 PhotoPainter firmware generates a new AP password at runtime when configuration mode starts: an 8-digit random numeric value. It is not a fixed default and is not derived from the SSID, MAC address, or chip ID; the password is shown on the device pairing/configuration screen.
- Connect to the AP and open the configuration page in a browser: `http://192.168.4.1/`
- Configure Wi-Fi and the InkTime server address. Approve the short-lived physical pairing code in the Web Devices page; the device completes recoverable claim/confirm before its normal workflow. Configure regular schedules in the Web console.

## Refresh and Sleep

Online devices validate a Queue or Release Manifest, exact payload length and SHA-256 before refreshing. `safe_4c` uses 96,000-byte 2bpp payloads; the six/seven-color profiles use 192,000-byte indexed4 payloads. Enhanced PhotoPainter can cache verified schedules on SD and use RTC-first offline slots. Identical validated content may skip a physical refresh while retaining the protocol ACK behavior.

PhotoPainter KEY1 double-click opens a read-only power page, holds it for 30 seconds after refresh, then restores the verified last successful SD frame. Long holds request forced network refresh or recovery as described in the device guide. GPIO0/5/21 are not repurposed for these actions.

Timeouts and the max-awake supervisor bound failure handling. Historical cold-start/KEY1 checks are documented separately; sleep current, timer accuracy and battery lifetime require measurements and are not guaranteed by source or CI.

## Related Projects

- ESP32 firmware depends on GxEPD2 © ZinggJM (GPL-3.0): https://github.com/ZinggJM/GxEPD2  
  If you distribute compiled firmware, please comply with GPL-3.0 as well.

- The offline Chinese city-name index in this project is built from GeoNames data:  
  GeoNames © GeoNames contributors, CC BY 4.0  
  https://www.geonames.org/

## Star History

<p align="center">
  <a href="https://star-history.com/#dai-hongtao/InkTime&Timeline">
    <img src="https://api.star-history.com/svg?repos=dai-hongtao/InkTime&type=Timeline" width="700"/>
  </a>
</p>
