#include "rs485_master.h"

#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "board_config.h"
#include "ch446.h"
#include "esp_random.h"
#include "rs485_bus.h"

#define RS485_MASTER_RESPONSE_TIMEOUT_MS 500U

static void format_status_hex(const uint8_t *bytes,
                              size_t byte_count,
                              char *text,
                              size_t text_size)
{
    static const char digits[] = "0123456789ABCDEF";
    if (text_size == 0U) {
        return;
    }
    size_t index = 0U;
    for (size_t byte = 0U; byte < byte_count && index + 2U < text_size;
         byte++) {
        text[index++] = digits[(bytes[byte] >> 4U) & 0x0FU];
        text[index++] = digits[bytes[byte] & 0x0FU];
    }
    text[index] = '\0';
}

static bool target_to_address(const char *target, uint8_t *address)
{
    if (target == NULL || address == NULL) {
        return false;
    }
    if (strncasecmp(target, "slave", 5U) == 0) {
        const char *digits = target + 5;
        unsigned number = 0U;
        if (*digits < '1' || *digits > '9') {
            return false;
        }
        for (; *digits != '\0'; digits++) {
            if (*digits < '0' || *digits > '9' || number > RS485_MAX_MODULES) {
                return false;
            }
            number = number * 10U + (unsigned)(*digits - '0');
        }
        if (number == 0U || number > RS485_MAX_MODULES) {
            return false;
        }
        *address = (uint8_t)(RS485_ID_SLAVE1 + number - 1U);
    } else if (strcasecmp(target, "broadcast") == 0 ||
               strcasecmp(target, "all") == 0) {
        *address = RS485_ID_BROADCAST;
    } else {
        return false;
    }
    return true;
}

static bool parse_bank(const char *text, ch446_bank_t *bank)
{
    if (strcasecmp(text, "S1") == 0) {
        *bank = CH446_BANK_S1;
        return true;
    }
    if (strcasecmp(text, "S2") == 0) {
        *bank = CH446_BANK_S2;
        return true;
    }
    return false;
}

static bool parse_bus(const char *text, ch446_bus_t *bus)
{
    if (text == NULL || bus == NULL ||
        (text[0] != 'Y' && text[0] != 'y') ||
        text[1] < '0' || text[1] > '4' || text[2] != '\0') {
        return false;
    }
    *bus = (ch446_bus_t)(text[1] - '0');
    return true;
}

static const char *bank_name(ch446_bank_t bank)
{
    return bank == CH446_BANK_S1 ? "S1" : "S2";
}

static esp_err_t send_command(uint8_t address,
                              uint8_t command,
                              const uint8_t *data,
                              uint8_t length,
                              rs485_frame_t *response)
{
    return rs485_bus_request(address,
                             command,
                             data,
                             length,
                             response,
                             RS485_MASTER_RESPONSE_TIMEOUT_MS);
}

esp_err_t rs485_master_start(void)
{
    return rs485_bus_init();
}

static esp_err_t topology_command(unsigned module_index0, uint8_t command,
                                  const uint8_t *data, uint8_t length,
                                  rs485_frame_t *response)
{
    if (module_index0 >= RS485_MAX_MODULES) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t error = ESP_ERR_TIMEOUT;
    for (unsigned attempt = 0; attempt < 2U; attempt++) {
        error = send_command((uint8_t)(RS485_ID_SLAVE1 + module_index0),
                             command, data, length, response);
        if (error != ESP_ERR_TIMEOUT && error != ESP_ERR_INVALID_CRC) {
            break;
        }
    }
    if (error != ESP_OK) {
        return error;
    }
    switch (response->data[0]) {
    case RS485_STATUS_OK:
        return ESP_OK;
    case RS485_STATUS_BAD_COMMAND:
        return ESP_ERR_NOT_SUPPORTED;
    case RS485_STATUS_BAD_ARGUMENT:
        return ESP_ERR_INVALID_ARG;
    case RS485_STATUS_BUSY:
        return ESP_ERR_INVALID_STATE;
    default:
        return ESP_FAIL;
    }
}

static void nonce_payload(uint8_t *payload)
{
    payload[0] = RS485_TOPOLOGY_VERSION;
    rs485_put_u32(payload + 1, esp_random());
}

esp_err_t rs485_master_probe_module(unsigned module_index0)
{
    uint8_t payload[RS485_NONCE_PAYLOAD_SIZE];
    nonce_payload(payload);
    rs485_frame_t response;
    esp_err_t error = topology_command(module_index0, RS485_CMD_CAPS,
                                       payload, sizeof(payload), &response);
    if (error != ESP_OK) {
        return error;
    }
    const unsigned offset = 1U + sizeof(payload);
    if (response.length != offset + RS485_CAPS_EXTRA_SIZE ||
        response.data[offset] != RS485_TOPOLOGY_VERSION ||
        response.data[offset + 1U] != RS485_GROUPS_PER_MODULE ||
        response.data[offset + 2U] != RS485_MAX_MODULES) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    return ESP_OK;
}

esp_err_t rs485_master_apply_mask(unsigned module_index0, uint32_t session,
                                uint32_t step, uint32_t mask, bool positive)
{
    if (session == 0U || (mask & ~RS485_GROUP_MASK) != 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t payload[RS485_MASK_PAYLOAD_SIZE];
    payload[0] = RS485_TOPOLOGY_VERSION;
    rs485_put_u32(payload + 1, session);
    rs485_put_u32(payload + 5, step);
    payload[9] = (uint8_t)mask;
    payload[10] = (uint8_t)(mask >> 8U);
    payload[11] = (uint8_t)(mask >> 16U);
    payload[12] = positive ? 1U : 0U;
    rs485_frame_t response;
    return topology_command(module_index0, RS485_CMD_MASK,
                            payload, sizeof(payload), &response);
}

esp_err_t rs485_master_reset_modules(unsigned count)
{
    if (count == 0U || count > RS485_MAX_MODULES) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t first_error = ESP_OK;
    for (unsigned module = 0; module < count; module++) {
        uint8_t payload[RS485_NONCE_PAYLOAD_SIZE];
        nonce_payload(payload);
        rs485_frame_t response;
        esp_err_t error = topology_command(module, RS485_CMD_CLEAR,
                                           payload, sizeof(payload), &response);
        if (first_error == ESP_OK && error != ESP_OK) {
            first_error = error;
        }
    }
    return first_error;
}

esp_err_t rs485_master_execute(const char *target_id,
                               const char *command,
                               char *response,
                               size_t response_size)
{
    if (response == NULL || response_size == 0U || command == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t address = 0U;
    if (!target_to_address(target_id, &address)) {
        snprintf(response, response_size, "ERR BUS invalid_target=%s",
                 target_id != NULL ? target_id : "-");
        return ESP_ERR_INVALID_ARG;
    }

    rs485_frame_t reply;
    uint8_t payload[4];
    uint8_t payload_length = 0U;
    uint8_t command_code = 0U;
    char command_name[12];
    char bank_text[4];
    char bus_text[4];
    char state_text[4];
    unsigned int x = 0U;
    unsigned int negative_x = 0U;
    int field_count = 0;
    char extra = '\0';
    ch446_bank_t bank;
    ch446_bus_t bus;
    ch446_bank_t negative_bank;

    if (strcasecmp(command, "PING") == 0) {
        command_code = RS485_CMD_PING;
    } else if (strcasecmp(command, "STATUS") == 0) {
        command_code = RS485_CMD_STATUS;
    } else if (strcasecmp(command, "RESET") == 0) {
        command_code = RS485_CMD_RESET;
    } else if (strncasecmp(command, "SWITCH ", 7U) == 0) {
        field_count = (unsigned int)sscanf(command,
                                            "%11s %3s %u %3s %3s %c",
                                            command_name,
                                            bank_text,
                                            &x,
                                            bus_text,
                                            state_text,
                                            &extra);
        if (field_count != 5 || strcasecmp(command_name, "SWITCH") != 0 ||
            x >= CH446_X_COUNT || !parse_bank(bank_text, &bank) ||
            !parse_bus(bus_text, &bus)) {
            snprintf(response, response_size,
                     "ERR SWITCH usage=SWITCH S1 0 Y4 ON|OFF");
            return ESP_ERR_INVALID_ARG;
        }
        if (strcasecmp(state_text, "ON") == 0) {
            payload[3] = 1U;
        } else if (strcasecmp(state_text, "OFF") == 0) {
            payload[3] = 0U;
        } else {
            snprintf(response, response_size,
                     "ERR SWITCH usage=SWITCH S1 0 Y4 ON|OFF");
            return ESP_ERR_INVALID_ARG;
        }
        payload[0] = (uint8_t)bank;
        payload[1] = (uint8_t)x;
        payload[2] = (uint8_t)bus;
        payload_length = 4U;
        command_code = RS485_CMD_SWITCH;
    } else if (strncasecmp(command, "CONNECT ", 8U) == 0) {
        char positive_bank_text[4];
        char negative_bank_text[4];
        field_count = (unsigned int)sscanf(command,
                                            "%11s %3s %u %3s %u %c",
                                            command_name,
                                            positive_bank_text,
                                            &x,
                                            negative_bank_text,
                                            &negative_x,
                                            &extra);
        if (field_count != 5 || strcasecmp(command_name, "CONNECT") != 0 ||
            x >= CH446_X_COUNT || negative_x >= CH446_X_COUNT ||
            !parse_bank(positive_bank_text, &bank) ||
            !parse_bank(negative_bank_text, &negative_bank) ||
            (bank == negative_bank && x == negative_x)) {
            snprintf(response, response_size,
                     "ERR CONNECT usage=CONNECT S1 0 S2 0");
            return ESP_ERR_INVALID_ARG;
        }
        payload[0] = (uint8_t)bank;
        payload[1] = (uint8_t)x;
        payload[2] = (uint8_t)negative_bank;
        payload[3] = (uint8_t)negative_x;
        payload_length = 4U;
        command_code = RS485_CMD_CONNECT;
    } else {
        snprintf(response, response_size, "ERR UNKNOWN_COMMAND");
        return ESP_ERR_NOT_SUPPORTED;
    }

    /* Broadcast frames have no response by protocol definition. */
    if (address == RS485_ID_BROADCAST) {
        rs485_frame_t request = {
            .address = address,
            .command = command_code,
            .length = payload_length,
        };
        if (payload_length > 0U) {
            memcpy(request.data, payload, payload_length);
        }
        const esp_err_t transport_error = rs485_bus_send(&request);
        if (transport_error != ESP_OK) {
            snprintf(response, response_size, "ERR BUS %s",
                     esp_err_to_name(transport_error));
            return transport_error;
        }
        if (command_code == RS485_CMD_PING) {
            snprintf(response, response_size, "OK BUS_SENT broadcast PING");
        } else if (command_code == RS485_CMD_RESET) {
            snprintf(response, response_size, "OK BUS_SENT broadcast RESET");
        } else if (command_code == RS485_CMD_SWITCH) {
            snprintf(response, response_size,
                     "OK BUS_SENT broadcast SWITCH %s %u Y%u %s",
                     bank_name((ch446_bank_t)payload[0]),
                     payload[1],
                     payload[2],
                     payload[3] != 0U ? "ON" : "OFF");
        } else if (command_code == RS485_CMD_STATUS) {
            snprintf(response, response_size, "OK BUS_SENT broadcast STATUS");
        } else {
            snprintf(response, response_size,
                     "OK BUS_SENT broadcast CONNECT %s %u %s %u",
                     bank_name((ch446_bank_t)payload[0]),
                     payload[1],
                     bank_name((ch446_bank_t)payload[2]),
                     payload[3]);
        }
        return ESP_OK;
    }

    const esp_err_t transport_error = send_command(address,
                                                   command_code,
                                                   payload,
                                                   payload_length,
                                                   &reply);
    if (transport_error != ESP_OK) {
        snprintf(response, response_size, "ERR BUS %s",
                 esp_err_to_name(transport_error));
        return transport_error;
    }
    if (reply.data[0] != RS485_STATUS_OK) {
        snprintf(response, response_size, "ERR BUS status=%u", reply.data[0]);
        return ESP_FAIL;
    }

    if (command_code == RS485_CMD_PING) {
        snprintf(response, response_size, "OK PONG");
    } else if (command_code == RS485_CMD_STATUS) {
        const size_t bytes_per_chip = CH446_STATUS_BYTES_PER_CHIP;
        if (reply.length < 1U + bytes_per_chip) {
            snprintf(response, response_size, "ERR BUS invalid_status_payload");
            return ESP_ERR_INVALID_RESPONSE;
        }
        char s1_hex[CH446_STATUS_BYTES_PER_CHIP * 2U + 1U];
        format_status_hex(&reply.data[1], bytes_per_chip,
                          s1_hex, sizeof(s1_hex));
        if (reply.length >= 1U + 2U * bytes_per_chip) {
            char s2_hex[CH446_STATUS_BYTES_PER_CHIP * 2U + 1U];
            format_status_hex(&reply.data[1U + bytes_per_chip], bytes_per_chip,
                              s2_hex, sizeof(s2_hex));
            snprintf(response, response_size,
                     "OK STATUS S1 %s S2 %s", s1_hex, s2_hex);
        } else {
            snprintf(response, response_size, "OK STATUS S1 %s", s1_hex);
        }
    } else if (command_code == RS485_CMD_RESET) {
        snprintf(response, response_size, "OK RESET");
    } else if (command_code == RS485_CMD_SWITCH) {
        snprintf(response, response_size, "OK SWITCH %s %u Y%u %s",
                 bank_name((ch446_bank_t)payload[0]),
                 payload[1],
                 payload[2],
                 payload[3] != 0U ? "ON" : "OFF");
    } else {
        snprintf(response, response_size, "OK CONNECT %s %u %s %u",
                 bank_name((ch446_bank_t)payload[0]),
                 payload[1],
                 bank_name((ch446_bank_t)payload[2]),
                 payload[3]);
    }
    return ESP_OK;
}
