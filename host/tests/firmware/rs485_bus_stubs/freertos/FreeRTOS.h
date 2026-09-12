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
/* The real firmware runs CONFIG_FREERTOS_HZ=100, so one tick is 10 ms. Keeping
 * that granularity matters here: it is what turns the 304 us inter-frame gap
 * into a one-tick wait and exercises the ceiling in rs485_silence_ticks(). */
#define pdMS_TO_TICKS(value) ((TickType_t)(((value) + 9U) / 10U))
#define portTICK_PERIOD_MS 10U
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(lock) ((void)(lock))
#define portEXIT_CRITICAL(lock) ((void)(lock))
