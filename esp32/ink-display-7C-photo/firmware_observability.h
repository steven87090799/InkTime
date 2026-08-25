#pragma once

#include <Arduino.h>

// 0=ERROR, 1=WARNING, 2=INFO, 3=DEBUG. Production emits only bounded
// lifecycle transitions. Never pass credentials, passwords, payload bytes,
// binary contents, headers, or credential-bearing URLs to these macros.
#ifndef INKTIME_LOG_LEVEL
#define INKTIME_LOG_LEVEL 2
#endif

#define INK_LOG_BEGIN() Serial.begin(115200)
#define INK_LOG_EVENT(required, level, event, message) do { \
  if (INKTIME_LOG_LEVEL >= (required)) { \
    Serial.print("["); Serial.print(level); Serial.print("] [firmware] ["); \
    Serial.print(event); Serial.print("] "); Serial.println(message); \
  } \
} while (0)
#define INK_LOG_ERROR(event, message) INK_LOG_EVENT(0, "ERROR", event, message)
#define INK_LOG_WARN(event, message)  INK_LOG_EVENT(1, "WARNING", event, message)
#define INK_LOG_INFO(event, message)  INK_LOG_EVENT(2, "INFO", event, message)
#define INK_LOG_DEBUG(event, message) INK_LOG_EVENT(3, "DEBUG", event, message)
