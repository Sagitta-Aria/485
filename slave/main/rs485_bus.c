#include "rs485_bus.h"

#include <stdbool.h>
#include <string.h>

#include "board_config.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define RS485_RX_BUFFER_SIZE 512U
#define RS485_TX_TIMEOUT_MS  100U

static bool s_initialized;
static SemaphoreHandle_t s_bus_mutex;

static uint16_t rs485_crc16(const uint8_t *data, size_t length)
{
    uint16_t crc = 0xFFFFU;
    for (size_t index = 0; index < length; index++) {
        crc ^= data[index];
        for (uint8_t bit = 0; bit < 8U; bit++) {
            crc = (crc & 1U) != 0U ? (uint16_t)((crc >> 1U) ^ 0xA001U)
                                   : (uint16_t)(crc >> 1U);
        }
    }
    return crc;
}

static void rs485_set_transmit(bool transmit)
{
    gpio_set_level(BOARD_RS485_DE_RE_GPIO, transmit ? 1 : 0);
}

static TickType_t rs485_timeout_ticks(uint32_t timeout_ms)
{
    TickType_t ticks = pdMS_TO_TICKS(timeout_ms);
    return ticks == 0 ? 1 : ticks;
}

static esp_err_t rs485_read_exact(uint8_t *buffer,
                                  size_t length,
                                  TickType_t timeout_ticks)
{
    const TickType_t start = xTaskGetTickCount();
    size_t received = 0U;
    while (received < length) {
        const TickType_t elapsed = xTaskGetTickCount() - start;
        if (elapsed >= timeout_ticks) {
            return ESP_ERR_TIMEOUT;
        }
        const int count = uart_read_bytes(
            BOARD_RS485_UART_PORT,
            buffer + received,
            length - received,
            timeout_ticks - elapsed);
        if (count <= 0) {
            return ESP_ERR_TIMEOUT;
        }
        received += (size_t)count;
    }
    return ESP_OK;
}

esp_err_t rs485_bus_init(void)
{
    if (s_initialized) {
        return ESP_OK;
    }

    const uart_config_t uart_config = {
        .baud_rate = BOARD_RS485_UART_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t error = uart_param_config(BOARD_RS485_UART_PORT, &uart_config);
    if (error != ESP_OK) {
        return error;
    }
    error = uart_set_pin(BOARD_RS485_UART_PORT,
                         BOARD_RS485_UART_TX_GPIO,
                         BOARD_RS485_UART_RX_GPIO,
                         UART_PIN_NO_CHANGE,
                         UART_PIN_NO_CHANGE);
    if (error != ESP_OK) {
        return error;
    }
    error = uart_driver_install(BOARD_RS485_UART_PORT,
                                RS485_RX_BUFFER_SIZE,
                                0,
                                0,
                                NULL,
                                0);
    if (error != ESP_OK) {
        return error;
    }

    gpio_config_t direction_config = {
        .pin_bit_mask = 1ULL << BOARD_RS485_DE_RE_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    error = gpio_config(&direction_config);
    if (error != ESP_OK) {
        uart_driver_delete(BOARD_RS485_UART_PORT);
        return error;
    }
    rs485_set_transmit(false);

    s_bus_mutex = xSemaphoreCreateRecursiveMutex();
    if (s_bus_mutex == NULL) {
        uart_driver_delete(BOARD_RS485_UART_PORT);
        return ESP_ERR_NO_MEM;
    }
    s_initialized = true;
    return ESP_OK;
}

esp_err_t rs485_bus_send(const rs485_frame_t *frame)
{
    if (!s_initialized || frame == NULL ||
        frame->length > RS485_MAX_DATA_LENGTH) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t encoded[1U + 3U + RS485_MAX_DATA_LENGTH + 2U];
    encoded[0] = RS485_SOF;
    encoded[1] = frame->address;
    encoded[2] = frame->command;
    encoded[3] = frame->length;
    memcpy(&encoded[4], frame->data, frame->length);
    const size_t body_length = 4U + frame->length;
    const uint16_t crc = rs485_crc16(encoded, body_length);
    encoded[body_length] = (uint8_t)(crc & 0xFFU);
    encoded[body_length + 1U] = (uint8_t)(crc >> 8U);
    const size_t frame_length = body_length + 2U;

    if (xSemaphoreTakeRecursive(s_bus_mutex, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    rs485_set_transmit(true);
    const int written = uart_write_bytes(BOARD_RS485_UART_PORT,
                                         encoded,
                                         frame_length);
    esp_err_t error = written == (int)frame_length ? ESP_OK : ESP_FAIL;
    if (error == ESP_OK) {
        error = uart_wait_tx_done(BOARD_RS485_UART_PORT,
                                  pdMS_TO_TICKS(RS485_TX_TIMEOUT_MS));
    }
    rs485_set_transmit(false);
    xSemaphoreGiveRecursive(s_bus_mutex);
    return error;
}

esp_err_t rs485_bus_receive(rs485_frame_t *frame, uint32_t timeout_ms)
{
    if (!s_initialized || frame == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(frame, 0, sizeof(*frame));
    const TickType_t timeout_ticks = rs485_timeout_ticks(timeout_ms);
    const TickType_t start = xTaskGetTickCount();
    uint8_t byte = 0U;
    while (true) {
        const TickType_t elapsed = xTaskGetTickCount() - start;
        if (elapsed >= timeout_ticks) {
            return ESP_ERR_TIMEOUT;
        }
        const int count = uart_read_bytes(BOARD_RS485_UART_PORT,
                                          &byte,
                                          1U,
                                          timeout_ticks - elapsed);
        if (count <= 0) {
            return ESP_ERR_TIMEOUT;
        }
        if (byte == RS485_SOF) {
            break;
        }
    }

    uint8_t header[3];
    const TickType_t header_elapsed = xTaskGetTickCount() - start;
    if (header_elapsed >= timeout_ticks) {
        return ESP_ERR_TIMEOUT;
    }
    esp_err_t error = rs485_read_exact(header,
                                       sizeof(header),
                                       timeout_ticks - header_elapsed);
    if (error != ESP_OK) {
        return error;
    }
    frame->address = header[0];
    frame->command = header[1];
    frame->length = header[2];
    if (frame->length > RS485_MAX_DATA_LENGTH) {
        return ESP_ERR_INVALID_SIZE;
    }

    uint8_t tail[RS485_MAX_DATA_LENGTH + 2U];
    const TickType_t elapsed = xTaskGetTickCount() - start;
    if (elapsed >= timeout_ticks) {
        return ESP_ERR_TIMEOUT;
    }
    error = rs485_read_exact(tail,
                             frame->length + 2U,
                             timeout_ticks - elapsed);
    if (error != ESP_OK) {
        return error;
    }
    memcpy(frame->data, tail, frame->length);

    uint8_t body[1U + 3U + RS485_MAX_DATA_LENGTH];
    body[0] = RS485_SOF;
    body[1] = frame->address;
    body[2] = frame->command;
    body[3] = frame->length;
    memcpy(&body[4], frame->data, frame->length);
    const uint16_t calculated = rs485_crc16(body, 4U + frame->length);
    const uint16_t received = (uint16_t)tail[frame->length] |
                              ((uint16_t)tail[frame->length + 1U] << 8U);
    return calculated == received ? ESP_OK : ESP_ERR_INVALID_CRC;
}

esp_err_t rs485_bus_request(uint8_t address,
                            uint8_t command,
                            const uint8_t *data,
                            uint8_t length,
                            rs485_frame_t *response,
                            uint32_t timeout_ms)
{
    if (!s_initialized || response == NULL || length > RS485_MAX_DATA_LENGTH ||
        (length != 0U && data == NULL)) {
        return ESP_ERR_INVALID_ARG;
    }
    rs485_frame_t request = {
        .address = address,
        .command = command,
        .length = length,
    };
    if (length > 0U) {
        memcpy(request.data, data, length);
    }
    if (xSemaphoreTakeRecursive(s_bus_mutex, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    esp_err_t error = uart_flush_input(BOARD_RS485_UART_PORT);
    if (error == ESP_OK) {
        error = rs485_bus_send(&request);
    }
    if (error != ESP_OK) {
        xSemaphoreGiveRecursive(s_bus_mutex);
        return error;
    }
    /* Hold the bus across send AND receive. New commands echo their complete
     * payload so delayed responses cannot acknowledge a different step.
     */
    const TickType_t start = xTaskGetTickCount();
    const TickType_t timeout_ticks = rs485_timeout_ticks(timeout_ms);
    error = ESP_ERR_TIMEOUT;
    while ((TickType_t)(xTaskGetTickCount() - start) < timeout_ticks) {
        const TickType_t elapsed = xTaskGetTickCount() - start;
        if (elapsed >= timeout_ticks) {
            break;
        }
        uint32_t remaining_ms = (timeout_ticks - elapsed) * portTICK_PERIOD_MS;
        error = rs485_bus_receive(response, remaining_ms);
        if (error != ESP_OK) {
            if (error == ESP_ERR_TIMEOUT) {
                break;
            }
            continue;
        }
        if (response->address != address ||
            response->command != (uint8_t)(command | RS485_COMMAND_RESPONSE) ||
            response->length < 1U) {
            error = ESP_ERR_TIMEOUT;
            continue;
        }
        if (rs485_is_topology_command(command)) {
            if (response->length == 1U &&
                response->data[0] == RS485_STATUS_BAD_COMMAND) {
                error = ESP_ERR_NOT_SUPPORTED;
                break;
            }
            if (!rs485_topology_echo_matches(data, length, response->data,
                                             response->length)) {
                error = ESP_ERR_TIMEOUT;
                continue;
            }
        }
        error = ESP_OK;
        break;
    }
    xSemaphoreGiveRecursive(s_bus_mutex);
    return error;
}
