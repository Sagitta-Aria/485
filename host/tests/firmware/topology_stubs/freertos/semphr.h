#pragma once
#include "FreeRTOS.h"
typedef struct fake_mutex *SemaphoreHandle_t;
SemaphoreHandle_t xSemaphoreCreateMutex(void);
BaseType_t xSemaphoreTake(SemaphoreHandle_t semaphore, TickType_t wait);
void xSemaphoreGive(SemaphoreHandle_t semaphore);
SemaphoreHandle_t xSemaphoreCreateRecursiveMutex(void);
BaseType_t xSemaphoreTakeRecursive(SemaphoreHandle_t semaphore, TickType_t wait);
void xSemaphoreGiveRecursive(SemaphoreHandle_t semaphore);
