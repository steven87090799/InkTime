#pragma once

#include <HardwareSerial.h>

// 0=ERROR, 1=WARNING, 2=INFO, 3=DEBUG. Production defaults to bounded
// lifecycle INFO; verbose branch/transport diagnostics require an explicit
// build flag. Never pass credentials, Wi-Fi passwords, payload bytes or URLs.
#ifndef INKTIME_LOG_LEVEL
#define INKTIME_LOG_LEVEL 2
#endif

extern HardwareSerial DebugSerial;

#define INK_LOG_BEGIN() DebugSerial.begin(115200)
#define INK_LOG_EVENT(required, level, event, message) do { \
  if (INKTIME_LOG_LEVEL >= (required)) { \
    DebugSerial.print("["); DebugSerial.print(level); DebugSerial.print("] [firmware] ["); \
    DebugSerial.print(event); DebugSerial.print("] "); DebugSerial.println(message); \
  } \
} while (0)
#define INK_LOG_ERROR(event, message) INK_LOG_EVENT(0, "ERROR", event, message)
#define INK_LOG_WARN(event, message)  INK_LOG_EVENT(1, "WARNING", event, message)
#define INK_LOG_INFO(event, message)  INK_LOG_EVENT(2, "INFO", event, message)
#define INK_LOG_DEBUG(event, message) INK_LOG_EVENT(3, "DEBUG", event, message)
