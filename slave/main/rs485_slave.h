#pragma once

#include <stdint.h>

#include "esp_err.h"

/* Start the slave RS485 receive task for one numeric node ID. */
esp_err_t rs485_slave_start(uint8_t node_id);

/* Hold across a complete Wi-Fi debug matrix operation. Returns invalid-state
 * while a topology lease owns the matrix. Unlock only after successful lock;
 * do not hold the lock across socket writes or other blocking network work.
 */
esp_err_t rs485_slave_debug_lock(void);
void rs485_slave_debug_unlock(void);
