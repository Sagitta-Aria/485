#include "../../shared/rs485_topology_protocol.h"

#define CHECK(condition) do { if (!(condition)) return __LINE__; } while (0)
#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

EXPORT int rs485_test_lease(void)
{
    rs485_mask_lease_t lease = {0};
    CHECK(rs485_mask_check(&lease, 0U, 1U, 1U, false) == RS485_MASK_BAD_ARGUMENT);
    CHECK(rs485_mask_check(&lease, 1U, 1U, 0x1000000UL, false) == RS485_MASK_BAD_ARGUMENT);
    CHECK(rs485_mask_check(&lease, 42U, 7U, 3U, false) == RS485_MASK_NEW);
    rs485_mask_commit(&lease, 42U, 7U, 3U, false, 100U);
    CHECK(rs485_mask_check(&lease, 42U, 7U, 3U, false) == RS485_MASK_RETRY);
    CHECK(rs485_mask_check(&lease, 42U, 7U, 1U, false) == RS485_MASK_BAD_ARGUMENT);
    CHECK(rs485_mask_check(&lease, 42U, 7U, 3U, true) == RS485_MASK_BAD_ARGUMENT);
    CHECK(rs485_mask_check(&lease, 42U, 6U, 3U, false) == RS485_MASK_STALE);
    CHECK(rs485_mask_check(&lease, 99U, 8U, 3U, false) == RS485_MASK_BUSY);
    CHECK(rs485_mask_check(&lease, 42U, 8U, 0U, false) == RS485_MASK_NEW);
    rs485_mask_commit(&lease, 42U, 8U, 0U, false, 200U);
    CHECK(lease.active);
    CHECK(!rs485_mask_expired(&lease, 30199U, 30000U));
    CHECK(rs485_mask_expired(&lease, 30200U, 30000U));
    rs485_mask_close(&lease);
    CHECK(!lease.active);
    CHECK(rs485_mask_check(&lease, 42U, 8U, 0U, false) == RS485_MASK_STALE);
    CHECK(rs485_mask_check(&lease, 42U, 9U, 1U, true) == RS485_MASK_NEW);
    CHECK(rs485_mask_check(&lease, 99U, 1U, 1U, true) == RS485_MASK_NEW);
    return 0;
}

EXPORT int rs485_test_clock_wrap(void)
{
    rs485_mask_lease_t lease = {0};
    rs485_mask_commit(&lease, 1U, 1U, 1U, true, 0xFFFFFFF0U);
    CHECK(!rs485_mask_expired(&lease, 0x0000000FU, 32U));
    CHECK(rs485_mask_expired(&lease, 0x00000010U, 32U));
    rs485_mask_commit(&lease, 1U, 1U, 1U, true, 0x00000010U);
    CHECK(!rs485_mask_expired(&lease, 0x00000020U, 32U));
    return 0;
}

EXPORT int rs485_test_payload(void)
{
    uint8_t bytes[4];
    rs485_put_u32(bytes, 0xFEDCBA98UL);
    CHECK(bytes[0] == 0x98 && bytes[1] == 0xBA && bytes[2] == 0xDC && bytes[3] == 0xFE);
    CHECK(rs485_get_u32(bytes) == 0xFEDCBA98UL);
    CHECK(rs485_get_mask(bytes) == 0xDCBA98UL);
    CHECK(RS485_MAX_MODULES == 10U);
    CHECK(RS485_GROUPS_PER_MODULE == 24U);
    return 0;
}

EXPORT int rs485_test_stale_ack(void)
{
    uint8_t request[RS485_MASK_PAYLOAD_SIZE] = {1, 2, 0, 0, 0, 3, 0, 0, 0, 1, 2, 3, 0};
    uint8_t response[1U + RS485_MASK_PAYLOAD_SIZE] = {0};
    for (unsigned i = 0; i < sizeof(request); i++) {
        response[i + 1U] = request[i];
    }
    CHECK(rs485_topology_echo_matches(request, sizeof(request), response, sizeof(response)));
    CHECK(!rs485_topology_echo_matches(request, sizeof(request), response, 1U));
    for (unsigned i = 0; i < sizeof(request); i++) {
        response[i + 1U] ^= 1U;
        CHECK(!rs485_topology_echo_matches(request, sizeof(request), response, sizeof(response)));
        response[i + 1U] ^= 1U;
    }
    response[0] = 3U;
    CHECK(rs485_topology_echo_matches(request, sizeof(request), response, sizeof(response)));
    uint8_t caps_request[RS485_NONCE_PAYLOAD_SIZE] = {1, 0x12, 0x34, 0x56, 0x78};
    uint8_t caps_response[1U + RS485_NONCE_PAYLOAD_SIZE + RS485_CAPS_EXTRA_SIZE] = {
        0, 1, 0x12, 0x34, 0x56, 0x78, 1, 24, 10
    };
    CHECK(rs485_topology_echo_matches(caps_request, sizeof(caps_request), caps_response, sizeof(caps_response)));
    caps_response[2] ^= 1U;
    CHECK(!rs485_topology_echo_matches(caps_request, sizeof(caps_request), caps_response, sizeof(caps_response)));
    return 0;
}
