#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#define TOPOLOGY_JOURNAL_PAYLOAD_MAX 224
#define TOPOLOGY_JOURNAL_OWNER_MAX 31

typedef struct {
    uint32_t session;
    uint32_t sequence;
    uint32_t job_id;
    uint32_t crc;
    char payload[TOPOLOGY_JOURNAL_PAYLOAD_MAX + 1];
} topology_journal_record_t;

typedef struct {
    uint32_t session;
    uint32_t ack;
    uint32_t ack_crc;
    uint32_t next;
    uint32_t first;
    uint32_t used_records;
    uint32_t capacity_records;
    char owner[TOPOLOGY_JOURNAL_OWNER_MAX + 1];
    bool recovered;
} topology_journal_info_t;

/* Task-context API; all operations serialize internally and can access flash.
 * init recovers committed records without discarding corrupt unacknowledged data.
 * A different session can be opened only after all previous records were ACKed.
 * CRC32 is IEEE/zlib over ASCII "<session> <sequence> <job_id> <payload>".
 */
esp_err_t topology_journal_init(void);
esp_err_t topology_journal_open(uint32_t session, const char *owner);
esp_err_t topology_journal_append(uint32_t job_id, const char *payload,
                                  uint32_t *sequence, uint32_t *wire_crc);
esp_err_t topology_journal_read(uint32_t sequence, topology_journal_record_t *record);
/* Cumulative ACK: the caller must have durably saved every sequence through it. */
esp_err_t topology_journal_ack(uint32_t sequence, uint32_t wire_crc);
esp_err_t topology_journal_get_info(topology_journal_info_t *info);
/* High-water latch: >=70% pauses; only <50% clears it. */
bool topology_journal_should_pause(void);
