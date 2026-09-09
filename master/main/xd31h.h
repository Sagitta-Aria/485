#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* At 9600 baud, allow enough time for a complete Modbus RTU response. */
#define XD31H_DEFAULT_TIMEOUT_MS 300U
#define XD31H_MAX_RESPONSE_LENGTH 9U
#define XD31H_DIAGNOSTIC_TEXT_SIZE 128U

typedef enum {
    XD31H_DIAGNOSTIC_NONE = 0,
    XD31H_DIAGNOSTIC_INVALID_ARGUMENT,
    XD31H_DIAGNOSTIC_NOT_INITIALIZED,
    XD31H_DIAGNOSTIC_UART_FLUSH,
    XD31H_DIAGNOSTIC_UART_WRITE,
    XD31H_DIAGNOSTIC_UART_TRANSMIT,
    XD31H_DIAGNOSTIC_UART_RECEIVE,
    XD31H_DIAGNOSTIC_TIMEOUT,
    XD31H_DIAGNOSTIC_RESPONSE_TOO_LARGE,
    XD31H_DIAGNOSTIC_FRAME_TOO_SHORT,
    XD31H_DIAGNOSTIC_CRC_MISMATCH,
    XD31H_DIAGNOSTIC_SLAVE_ADDRESS,
    XD31H_DIAGNOSTIC_MODBUS_EXCEPTION,
    XD31H_DIAGNOSTIC_RESPONSE_FORMAT,
    XD31H_DIAGNOSTIC_STATUS_OL,
    XD31H_DIAGNOSTIC_STATUS_UNKNOWN,
    XD31H_DIAGNOSTIC_INVALID_RANGE,
} xd31h_diagnostic_t;

typedef struct {
    bool valid;
    uint8_t status;
    uint8_t range;
    uint16_t raw_value;
    float resistance_ohm;
    xd31h_diagnostic_t diagnostic;
    int32_t diagnostic_value;
    uint16_t received_crc;
    uint16_t calculated_crc;
    size_t response_length;
    uint8_t response[XD31H_MAX_RESPONSE_LENGTH];
} xd31h_measurement_t;

/* Initialize the UART used by the XD31H module. Call this once at startup. */
esp_err_t xd31h_init(void);

/*
 * Send one Modbus RTU query and wait for one measurement response.
 * This function is blocking and must not be called concurrently.
 */
esp_err_t xd31h_read_measurement(xd31h_measurement_t *measurement, uint32_t timeout_ms);

/*
 * Format the exact XD31H failure or non-zero measurement status as one ASCII
 * protocol payload. Use only when read_measurement() failed or valid is false.
 */
void xd31h_format_diagnostic(const xd31h_measurement_t *measurement,
                             esp_err_t error,
                             char *buffer,
                             size_t buffer_size);
