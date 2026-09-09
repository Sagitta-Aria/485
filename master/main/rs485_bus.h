#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "../../shared/rs485_topology_protocol.h"

#define RS485_SOF                    0xA5U
#define RS485_MAX_DATA_LENGTH        32U
#define RS485_COMMAND_RESPONSE       0x80U

#define RS485_ID_MASTER              0x01U
#define RS485_ID_SLAVE1              0x11U
#define RS485_ID_SLAVE2              0x12U
#define RS485_ID_SLAVE3              0x13U
#define RS485_ID_SLAVE4              0x14U
#define RS485_ID_SLAVE10             0x1AU
#define RS485_ID_BROADCAST           0xFFU

#define RS485_CMD_RESET              0x01U
#define RS485_CMD_PING               0x02U
#define RS485_CMD_STATUS             0x03U
#define RS485_CMD_SWITCH             0x10U
#define RS485_CMD_CONNECT            0x11U

#define RS485_STATUS_OK              0x00U
#define RS485_STATUS_BAD_COMMAND     0x01U
#define RS485_STATUS_BAD_ARGUMENT    0x02U
#define RS485_STATUS_BUSY            0x03U
#define RS485_STATUS_INTERNAL        0x04U

typedef struct {
    uint8_t address;
    uint8_t command;
    uint8_t length;
    uint8_t data[RS485_MAX_DATA_LENGTH];
} rs485_frame_t;

/* Initialize the UART and the half-duplex transceiver direction pin. */
esp_err_t rs485_bus_init(void);

/* Send one complete frame and return after the UART has shifted every byte. */
esp_err_t rs485_bus_send(const rs485_frame_t *frame);

/* Receive and validate one frame, resynchronizing at the SOF byte. */
esp_err_t rs485_bus_receive(rs485_frame_t *frame, uint32_t timeout_ms);

/* Send one request and wait for the matching response frame. */
esp_err_t rs485_bus_request(uint8_t address,
                            uint8_t command,
                            const uint8_t *data,
                            uint8_t length,
                            rs485_frame_t *response,
                            uint32_t timeout_ms);
