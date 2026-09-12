#pragma once
#include "FreeRTOS.h"
typedef void (*TaskFunction_t)(void *);
BaseType_t xTaskCreate(TaskFunction_t function, const char *name, unsigned stack,
                       void *argument, unsigned priority, void *handle);
void vTaskDelay(TickType_t ticks);
void vTaskDelete(void *task);
