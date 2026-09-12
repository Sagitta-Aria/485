#pragma once
#include <stddef.h>
#include <stdint.h>
typedef unsigned TickType_t;
typedef int BaseType_t;
typedef int portMUX_TYPE;
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define portMAX_DELAY UINT32_MAX
#define pdMS_TO_TICKS(value) ((TickType_t)(value))
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(lock) ((void)(lock))
#define portEXIT_CRITICAL(lock) ((void)(lock))
