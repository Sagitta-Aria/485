#pragma once
#include "FreeRTOS.h"
typedef struct fake_queue *QueueHandle_t;
QueueHandle_t xQueueCreate(unsigned capacity, unsigned item_size);
BaseType_t xQueueSend(QueueHandle_t queue, const void *item, TickType_t wait);
BaseType_t xQueueReceive(QueueHandle_t queue, void *item, TickType_t wait);
void xQueueReset(QueueHandle_t queue);
