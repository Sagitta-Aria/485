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

/* Configure the master U1/S1 chip and leave every crosspoint open. */
esp_err_t ch446_init(void);

/* Clear selectable contacts and restore any enabled fixed Kelvin path. */
esp_err_t ch446_reset_all(void);

/* Boot-time configuration: master1 holds X0-Y0 through X3-Y3 closed. */
esp_err_t ch446_set_fixed_kelvin(bool enabled);
bool ch446_fixed_kelvin_enabled(void);

/* Control one raw X-Y crosspoint; not ISR-safe. Fixed X0..X3 may only
 * close onto their matching Y. Extra legacy contacts may still be opened. */
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

/* Reset and establish a four-wire path between two selectable S1 ports.
 * Reject fixed instrument X0..X3 before changing any contacts. */
esp_err_t ch446_connect_kelvin_pair(
    ch446_port_t positive_port,
    ch446_port_t negative_port);

/* Copy the software crosspoint shadow under the matrix lock. */
esp_err_t ch446_get_status(ch446_matrix_status_t *status);
