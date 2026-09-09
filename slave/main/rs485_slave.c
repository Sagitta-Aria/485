#include "rs485_slave.h"

#include <stdbool.h>
#include <string.h>

#include "board_config.h"
#include "ch446.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "rs485_bus.h"

#define RS485_SLAVE_TASK_STACK_SIZE 4096U
#define RS485_SLAVE_TASK_PRIORITY   6U
#define RS485_SLAVE_RECEIVE_TIMEOUT_MS 100U
static uint8_t s_node_id;
static bool s_started;
static SemaphoreHandle_t s_control_mutex;
static rs485_mask_lease_t s_lease;

static void rs485_slave_expire_locked(void)
{
    if (rs485_mask_expired(&s_lease, (uint32_t)xTaskGetTickCount(),
                          pdMS_TO_TICKS(BOARD_SLAVE_MASK_LEASE_MS))) {
        if (ch446_reset_all() == ESP_OK) {
            rs485_mask_close(&s_lease);
            ESP_LOGW("rs485_slave", "Topology lease expired; matrix open");
        }
    }
}

static uint8_t rs485_slave_apply_mask(const rs485_frame_t *request)
{
    if (request->length != RS485_MASK_PAYLOAD_SIZE ||
        request->data[0] != RS485_TOPOLOGY_VERSION || request->data[12] > 1U) {
        return RS485_STATUS_BAD_ARGUMENT;
    }
    const uint32_t session = rs485_get_u32(request->data + 1);
    const uint32_t step = rs485_get_u32(request->data + 5);
    const uint32_t mask = rs485_get_mask(request->data + 9);
    const bool positive = request->data[12] != 0U;
    const rs485_mask_decision_t decision = rs485_mask_check(
        &s_lease, session, step, mask, positive);
    if (decision == RS485_MASK_STALE || decision == RS485_MASK_BUSY) {
        return RS485_STATUS_BUSY;
    }
    if (decision == RS485_MASK_BAD_ARGUMENT) {
        return RS485_STATUS_BAD_ARGUMENT;
    }
    if (decision == RS485_MASK_NEW &&
        ch446_apply_kelvin_mask(mask, positive) != ESP_OK) {
        ch446_reset_all();
        rs485_mask_close(&s_lease);
        return RS485_STATUS_INTERNAL;
    }
    rs485_mask_commit(&s_lease, session, step, mask, positive,
                      (uint32_t)xTaskGetTickCount());
    return RS485_STATUS_OK;
}

static uint8_t rs485_slave_dispatch(const rs485_frame_t *request)
{
    if (request->command == RS485_CMD_MASK) {
        return rs485_slave_apply_mask(request);
    }
    if (request->command == RS485_CMD_CAPS ||
        request->command == RS485_CMD_CLEAR) {
        if (request->length != RS485_NONCE_PAYLOAD_SIZE ||
            request->data[0] != RS485_TOPOLOGY_VERSION) {
            return RS485_STATUS_BAD_ARGUMENT;
        }
        if (request->command == RS485_CMD_CLEAR) {
            if (ch446_reset_all() != ESP_OK) {
                return RS485_STATUS_INTERNAL;
            }
            rs485_mask_close(&s_lease);
        }
        return RS485_STATUS_OK;
    }
    if (request->command == RS485_CMD_PING) {
        return request->length == 0U ? RS485_STATUS_OK
                                     : RS485_STATUS_BAD_ARGUMENT;
    }
    if (request->command == RS485_CMD_STATUS) {
        return request->length == 0U ? RS485_STATUS_OK
                                     : RS485_STATUS_BAD_ARGUMENT;
    }
    if (request->command == RS485_CMD_RESET) {
        if (request->length != 0U) {
            return RS485_STATUS_BAD_ARGUMENT;
        }
        if (ch446_reset_all() != ESP_OK) {
            return RS485_STATUS_INTERNAL;
        }
        rs485_mask_close(&s_lease);
        return RS485_STATUS_OK;
    }
    if (s_lease.active && (request->command == RS485_CMD_SWITCH ||
                           request->command == RS485_CMD_CONNECT)) {
        return RS485_STATUS_BUSY;
    }
    if (request->command == RS485_CMD_SWITCH) {
        if (request->length != 4U || request->data[0] >= CH446_BANK_COUNT ||
            request->data[1] >= CH446_X_COUNT ||
            request->data[2] >= CH446_BUS_COUNT ||
            request->data[3] > 1U) {
            return RS485_STATUS_BAD_ARGUMENT;
        }
        const ch446_port_t port = {
            .bank = (ch446_bank_t)request->data[0],
            .x = request->data[1],
        };
        return ch446_set_port_bus(port,
                                  (ch446_bus_t)request->data[2],
                                  request->data[3] != 0U) == ESP_OK
                   ? RS485_STATUS_OK
                   : RS485_STATUS_INTERNAL;
    }
    if (request->command == RS485_CMD_CONNECT) {
        if (request->length != 4U || request->data[0] >= CH446_BANK_COUNT ||
            request->data[1] >= CH446_X_COUNT ||
            request->data[2] >= CH446_BANK_COUNT ||
            request->data[3] >= CH446_X_COUNT ||
            (request->data[0] == request->data[2] &&
             request->data[1] == request->data[3])) {
            return RS485_STATUS_BAD_ARGUMENT;
        }
        const ch446_port_t positive = {
            .bank = (ch446_bank_t)request->data[0],
            .x = request->data[1],
        };
        const ch446_port_t negative = {
            .bank = (ch446_bank_t)request->data[2],
            .x = request->data[3],
        };
        return ch446_connect_kelvin_pair(positive, negative) == ESP_OK
                   ? RS485_STATUS_OK
                   : RS485_STATUS_INTERNAL;
    }
    return RS485_STATUS_BAD_COMMAND;
}

static void rs485_slave_task(void *argument)
{
    (void)argument;
    while (true) {
        /* Check on every pass, including traffic for other addresses, noise,
         * and repeated PING/STATUS. Only an accepted MASK renews the lease.
         */
        xSemaphoreTake(s_control_mutex, portMAX_DELAY);
        rs485_slave_expire_locked();
        xSemaphoreGive(s_control_mutex);
        rs485_frame_t request;
        const esp_err_t error = rs485_bus_receive(
            &request, RS485_SLAVE_RECEIVE_TIMEOUT_MS);
        if (error != ESP_OK) {
            continue;
        }
        if ((request.command & RS485_COMMAND_RESPONSE) != 0U ||
            (request.address != s_node_id &&
             request.address != RS485_ID_BROADCAST)) {
            continue;
        }
        xSemaphoreTake(s_control_mutex, portMAX_DELAY);
        rs485_slave_expire_locked();
        uint8_t status = rs485_slave_dispatch(&request);
        rs485_frame_t response = {
            .address = request.address,
            .command = (uint8_t)(request.command | RS485_COMMAND_RESPONSE),
            .length = 1U,
            .data = {status},
        };
        if (request.address != RS485_ID_BROADCAST) {
            if (rs485_is_topology_command(request.command) &&
                request.length < RS485_MAX_DATA_LENGTH) {
                response.length = (uint8_t)(1U + request.length);
                memcpy(response.data + 1, request.data, request.length);
                if (status == RS485_STATUS_OK && request.command == RS485_CMD_CAPS) {
                    response.data[response.length++] = RS485_TOPOLOGY_VERSION;
                    response.data[response.length++] = RS485_GROUPS_PER_MODULE;
                    response.data[response.length++] = RS485_MAX_MODULES;
                }
            } else if (status == RS485_STATUS_OK &&
                request.command == RS485_CMD_STATUS) {
                ch446_matrix_status_t matrix_status;
                if (ch446_get_status(&matrix_status) != ESP_OK) {
                    status = RS485_STATUS_INTERNAL;
                    response.data[0] = status;
                } else {
                    response.length = (uint8_t)(1U +
                        CH446_CHIP_COUNT * CH446_STATUS_BYTES_PER_CHIP);
                    memcpy(&response.data[1],
                           matrix_status.chip,
                           CH446_CHIP_COUNT * CH446_STATUS_BYTES_PER_CHIP);
                }
            }
        }
        xSemaphoreGive(s_control_mutex);
        if (request.address != RS485_ID_BROADCAST) {
            (void)rs485_bus_send(&response);
        }
    }
}

esp_err_t rs485_slave_start(uint8_t node_id)
{
    if (node_id < RS485_ID_SLAVE1 || node_id > RS485_ID_SLAVE10) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_started) {
        return node_id == s_node_id ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    if (s_control_mutex == NULL) {
        s_control_mutex = xSemaphoreCreateMutex();
        if (s_control_mutex == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    esp_err_t error = rs485_bus_init();
    if (error != ESP_OK) {
        return error;
    }
    s_node_id = node_id;
    if (xTaskCreate(rs485_slave_task,
                    "rs485_slave",
                    RS485_SLAVE_TASK_STACK_SIZE,
                    NULL,
                    RS485_SLAVE_TASK_PRIORITY,
                    NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    s_started = true;
    return ESP_OK;
}

esp_err_t rs485_slave_debug_lock(void)
{
    if (s_control_mutex == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (xSemaphoreTake(s_control_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    rs485_slave_expire_locked();
    if (s_lease.active) {
        xSemaphoreGive(s_control_mutex);
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

void rs485_slave_debug_unlock(void)
{
    xSemaphoreGive(s_control_mutex);
}
