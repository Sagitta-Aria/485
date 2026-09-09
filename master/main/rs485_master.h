#pragma once

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "../../shared/rs485_topology_protocol.h"

/* Initialize the master-side RS485 request/response transport. */
esp_err_t rs485_master_start(void);

/* Translate one legacy text command into a compact RS485 frame. */
esp_err_t rs485_master_execute(const char *target_id,
                               const char *command,
                               char *response,
                               size_t response_size);

/* Address module0..9 as slave1..slave10, retaining the existing numeric IDs.
 * Probe requires topology protocol support, not merely a legacy PING reply.
 */
esp_err_t rs485_master_probe_module(unsigned module_index0);

/* Replace one slave's 24 Kelvin groups. A nonzero session owns the matrix;
 * step must increase for new masks and after CLEAR. Retrying an identical
 * active mask is idempotent. A zero mask opens contacts but retains the lease.
 */
esp_err_t rs485_master_apply_mask(unsigned module_index0, uint32_t session,
                                uint32_t step, uint32_t mask, bool positive);

/* Correlated unicast CLEAR attempts every module and returns the first error. */
esp_err_t rs485_master_reset_modules(unsigned count);
