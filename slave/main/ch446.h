#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define CH446_X_COUNT              24U
#define CH446_Y_COUNT              5U
#define CH446_CHIP_COUNT           2U
#define CH446_STATUS_BYTES_PER_CHIP ((CH446_X_COUNT * CH446_Y_COUNT + 7U) / 8U)

typedef enum {
    CH446_CHIP_U1 = 0,
    CH446_CHIP_U2,
} ch446_chip_t;

typedef enum {
    CH446_BANK_S1 = 0,
    CH446_BANK_S2,
    CH446_BANK_COUNT,
} ch446_bank_t;

typedef struct {
    ch446_bank_t bank;
    uint8_t x;
} ch446_port_t;

typedef enum {
    CH446_BUS_I_MINUS = 0,
    CH446_BUS_V_MINUS = 1,
    CH446_BUS_V_PLUS = 2,
    CH446_BUS_I_PLUS = 3,
    CH446_BUS_Y4 = 4,
    CH446_BUS_COUNT,
} ch446_bus_t;

/* Software shadow of the crosspoints last written to each CH446 chip. */
typedef struct {
    uint8_t chip[CH446_CHIP_COUNT][CH446_STATUS_BYTES_PER_CHIP];
} ch446_matrix_status_t;

/* Configure both chips and leave every crosspoint open. */
esp_err_t ch446_init(void);

/* Open every crosspoint on both chips. */
esp_err_t ch446_reset_all(void);

/* Control one raw X-Y crosspoint. This function is not ISR-safe. */
esp_err_t ch446_set_crosspoint(
    ch446_chip_t chip,
    uint8_t x,
    uint8_t y,
    bool closed);

/* Connect one silkscreened S1_Xn or S2_Xn port to one shared Y measurement bus. */
esp_err_t ch446_set_port_bus(
    ch446_port_t port,
    ch446_bus_t bus,
    bool closed);

/* Reset the matrix and establish a four-wire path between two S1/S2 ports. */
esp_err_t ch446_connect_kelvin_pair(
    ch446_port_t positive_port,
    ch446_port_t negative_port);

/* Copy the software crosspoint shadow under the matrix lock. */
esp_err_t ch446_get_status(ch446_matrix_status_t *status);

/* Replace all crosspoints with a 24-group Kelvin mask. Group0..11 maps to
 * S1 X0/1..X22/23; group12..23 maps to S2. Even X is current, odd X voltage,
 * matching four_wire_loop_test.py. Voltage closes before current. Zero opens
 * everything. The complete operation holds the existing recursive mutex.
 */
esp_err_t ch446_apply_kelvin_mask(uint32_t mask, bool positive);
