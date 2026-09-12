#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Extension payloads preserve the existing A5/address/command/length/CRC frame.
 * MASK: version, session LE32, step LE32, mask LE24, positive byte.
 * CAPS/CLEAR: version, nonce LE32. Each response echoes the request payload
 * after its status byte. CAPS appends version, groups per module, max modules.
 */
#define RS485_MAX_MODULES 10U
#define RS485_GROUPS_PER_MODULE 24U
#define RS485_GROUP_MASK 0x00FFFFFFUL
#define RS485_TOPOLOGY_VERSION 1U
#define RS485_CMD_MASK 0x12U
#define RS485_CMD_CAPS 0x13U
#define RS485_CMD_CLEAR 0x14U
#define RS485_MASK_PAYLOAD_SIZE 13U
#define RS485_NONCE_PAYLOAD_SIZE 5U
#define RS485_CAPS_EXTRA_SIZE 3U

static inline void rs485_put_u32(uint8_t *bytes, uint32_t value)
{
    for (unsigned i = 0; i < 4U; i++) {
        bytes[i] = (uint8_t)(value >> (8U * i));
    }
}

static inline uint32_t rs485_get_u32(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8U) |
           ((uint32_t)bytes[2] << 16U) | ((uint32_t)bytes[3] << 24U);
}

static inline uint32_t rs485_get_mask(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8U) |
           ((uint32_t)bytes[2] << 16U);
}

static inline bool rs485_is_topology_command(uint8_t command)
{
    return command == RS485_CMD_MASK || command == RS485_CMD_CAPS ||
           command == RS485_CMD_CLEAR;
}

/* The echo starts after the response status byte; CAPS may append metadata. */
static inline bool rs485_topology_echo_matches(const uint8_t *request,
    uint8_t request_length, const uint8_t *response, uint8_t response_length)
{
    if (response_length < 1U + request_length) {
        return false;
    }
    for (unsigned i = 0U; i < request_length; i++) {
        if (response[i + 1U] != request[i]) {
            return false;
        }
    }
    return true;
}

typedef struct {
    bool active;
    bool known;
    bool closed;
    uint32_t session;
    uint32_t step;
    uint32_t mask;
    bool positive;
    uint32_t updated;
} rs485_mask_lease_t;

typedef enum {
    RS485_MASK_NEW,
    RS485_MASK_RETRY,
    RS485_MASK_STALE,
    RS485_MASK_BUSY,
    RS485_MASK_BAD_ARGUMENT,
} rs485_mask_decision_t;

/* Validate freshness without changing the lease or touching hardware. */
static inline rs485_mask_decision_t rs485_mask_check(
    const rs485_mask_lease_t *lease, uint32_t session, uint32_t step,
    uint32_t mask, bool positive)
{
    if (session == 0U || (mask & ~RS485_GROUP_MASK) != 0U) {
        return RS485_MASK_BAD_ARGUMENT;
    }
    if (lease->active && session != lease->session) {
        return RS485_MASK_BUSY;
    }
    if (lease->known && session == lease->session) {
        if (step < lease->step || (step == lease->step && lease->closed)) {
            return RS485_MASK_STALE;
        }
        if (step == lease->step) {
            return mask == lease->mask && positive == lease->positive
                ? RS485_MASK_RETRY : RS485_MASK_BAD_ARGUMENT;
        }
    }
    return RS485_MASK_NEW;
}

/* Call only after hardware applied successfully, or for an identical retry. */
static inline void rs485_mask_commit(rs485_mask_lease_t *lease,
    uint32_t session, uint32_t step, uint32_t mask, bool positive, uint32_t now)
{
    lease->active = true;
    lease->known = true;
    lease->closed = false;
    lease->session = session;
    lease->step = step;
    lease->mask = mask;
    lease->positive = positive;
    lease->updated = now;
}

static inline bool rs485_mask_expired(const rs485_mask_lease_t *lease,
                                     uint32_t now, uint32_t timeout)
{
    return lease->active && (uint32_t)(now - lease->updated) >= timeout;
}

/* Preserve the last step so a delayed retry cannot reclose a cleared path. */
static inline void rs485_mask_close(rs485_mask_lease_t *lease)
{
    lease->active = false;
    lease->closed = true;
}
