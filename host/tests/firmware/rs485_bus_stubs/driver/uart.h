#pragma once
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "esp_err.h"

/* Enum values are irrelevant to the framing logic; only the types matter so
 * that board_config.h and rs485_bus.c compile unchanged against this stub. */
typedef enum { RS485_FAKE_UART0 = 0, RS485_FAKE_UART1, RS485_FAKE_UART2 } uart_port_t;
typedef enum { UART_DATA_8_BITS } uart_word_length_t;
typedef enum { UART_PARITY_DISABLE } uart_parity_t;
typedef enum { UART_STOP_BITS_1 } uart_stop_bits_t;
typedef enum { UART_HW_FLOWCTRL_DISABLE } uart_hw_flowcontrol_t;
typedef enum { UART_SCLK_DEFAULT } uart_sclk_t;
#define UART_PIN_NO_CHANGE (-1)
#define UART_NUM_1 1
#define UART_NUM_2 2

typedef struct {
    int baud_rate;
    uart_word_length_t data_bits;
    uart_parity_t parity;
    uart_stop_bits_t stop_bits;
    uart_hw_flowcontrol_t flow_ctrl;
    uart_sclk_t source_clk;
} uart_config_t;

int uart_read_bytes(uart_port_t port, void *buffer, uint32_t length, TickType_t wait);
int uart_write_bytes(uart_port_t port, const void *source, size_t length);
esp_err_t uart_wait_tx_done(uart_port_t port, TickType_t wait);
esp_err_t uart_flush_input(uart_port_t port);
esp_err_t uart_param_config(uart_port_t port, const uart_config_t *config);
esp_err_t uart_set_pin(uart_port_t port, int tx, int rx, int rts, int cts);
esp_err_t uart_driver_install(uart_port_t port, int rx_buffer, int tx_buffer, int queue, void *handle, int flags);
esp_err_t uart_driver_delete(uart_port_t port);
