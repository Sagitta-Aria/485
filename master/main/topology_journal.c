#include "topology_journal.h"

#include <inttypes.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_partition.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define SECTOR_SIZE 4096U
#define SLOT_SIZE 256U
#define SLOTS_PER_SECTOR (SECTOR_SIZE / SLOT_SIZE)
#define META_SECTORS 2U
#define META_MAGIC UINT32_C(0x544a4d31)
#define META_PAUSED UINT32_C(0x00000002)
#define DATA_MAGIC UINT32_C(0x544a4431)
#define COMMIT_MAGIC UINT32_C(0x434f4d31)
#define NO_SECTOR UINT32_MAX

typedef struct {
    uint32_t magic, generation, session, ack, ack_crc, erase_sector;
    char owner[32];
    uint32_t crc, commit;
} journal_meta_t;

typedef struct {
    uint32_t magic, session, sequence, job_id, crc;
    uint16_t length, reserved;
    char payload[225];
    uint8_t padding[3];
    uint32_t commit;
} journal_slot_t;

_Static_assert(sizeof(journal_meta_t) == 64, "metadata slot layout");
_Static_assert(sizeof(journal_slot_t) == SLOT_SIZE, "data slot layout");

static const esp_partition_t *s_partition;
static SemaphoreHandle_t s_mutex;
static journal_meta_t s_meta;
static uint32_t s_meta_offset, s_next, s_capacity, s_sector_count, s_used;
static uint16_t *s_sector_used;
static uint32_t *s_sector_max;
static bool s_ready, s_recovered, s_paused;

static uint32_t crc_extend(uint32_t crc, const void *buffer, size_t length)
{
    const uint8_t *bytes = buffer;
    while (length--) {
        crc ^= *bytes++;
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (UINT32_C(0xedb88320) & (0U - (crc & 1U)));
    }
    return crc;
}

static uint32_t wire_crc(uint32_t session, uint32_t sequence, uint32_t job,
                         const char *payload)
{
    char prefix[40];
    int length = snprintf(prefix, sizeof(prefix), "%" PRIu32 " %" PRIu32 " %" PRIu32 " ",
                          session, sequence, job);
    uint32_t crc = crc_extend(UINT32_MAX, prefix, (size_t)length);
    return ~crc_extend(crc, payload, strlen(payload));
}

static bool blank(const void *buffer, size_t length)
{
    const uint8_t *bytes = buffer;
    while (length--) if (*bytes++ != 0xff) return false;
    return true;
}

static bool valid_text(const char *text, size_t maximum, bool owner)
{
    if (!text || !*text) return false;
    size_t length = 0;
    for (; length <= maximum && text[length]; ++length) {
        unsigned char value = (unsigned char)text[length];
        if (value < (owner ? 33 : 32) || value > 126) return false;
    }
    return length <= maximum;
}

static size_t data_offset(uint32_t index)
{
    return META_SECTORS * SECTOR_SIZE + (size_t)index * SLOT_SIZE;
}

static esp_err_t read_slot(uint32_t index, journal_slot_t *slot)
{
    return esp_partition_read(s_partition, data_offset(index), slot, sizeof(*slot));
}

static bool valid_slot(const journal_slot_t *slot)
{
    return slot->magic == DATA_MAGIC && slot->commit == COMMIT_MAGIC &&
           slot->session && slot->sequence && slot->reserved == 0 &&
           slot->length <= TOPOLOGY_JOURNAL_PAYLOAD_MAX &&
           slot->payload[slot->length] == '\0' &&
           valid_text(slot->payload, slot->length, false) &&
           strlen(slot->payload) == slot->length &&
           slot->crc == wire_crc(slot->session, slot->sequence, slot->job_id, slot->payload);
}

/* A torn NOR program can only leave intended zero bits still one. Clearing an
 * intended one cannot be a partial commit and must never make a record disposable. */
static bool partial_commit(uint32_t commit)
{
    return (commit & COMMIT_MAGIC) == COMMIT_MAGIC;
}

static void update_watermark(void)
{
    if ((uint64_t)s_used * 100 >= (uint64_t)s_capacity * 70) s_paused = true;
    else if ((uint64_t)s_used * 100 < (uint64_t)s_capacity * 50) s_paused = false;
}

/* Metadata is appended within two alternating sectors. The last committed
 * generation stays intact until the next generation's commit reaches flash:
 * erase_sector() persists its erase intent into the still-current bank before
 * that bank's successor is erased, so recovery always has either a valid
 * generation or a valid intent to finish. A review once suspected this order
 * discarded records; journal_test_metadata_switch_keeps_unacked_records cuts
 * power at every byte of the switch and proves it does not. */
static esp_err_t save_meta(journal_meta_t candidate)
{
    if (s_meta.generation == UINT32_MAX) return ESP_ERR_INVALID_STATE;
    uint32_t bank = s_meta_offset / SECTOR_SIZE;
    uint32_t offset = s_meta_offset + sizeof(candidate);
    journal_meta_t probe;
    esp_err_t error;
    while (offset < (bank + 1U) * SECTOR_SIZE) {
        error = esp_partition_read(s_partition, offset, &probe, sizeof(probe));
        if (error != ESP_OK) return error;
        if (blank(&probe, sizeof(probe))) break;
        offset += sizeof(probe);
    }
    if (offset >= (bank + 1U) * SECTOR_SIZE) {
        offset = (1U - bank) * SECTOR_SIZE;
        /* Retire old commit words before erasing their bank. A power cut during
         * erase then leaves only uncommitted debris beside the intact bank. */
        const uint32_t retired = 0;
        for (uint32_t item = offset; item < offset + SECTOR_SIZE; item += sizeof(candidate)) {
            error = esp_partition_write(s_partition, item + offsetof(journal_meta_t, commit),
                                         &retired, sizeof(retired));
            if (error != ESP_OK) { s_ready = false; return error; }
        }
        error = esp_partition_erase_range(s_partition, offset, SECTOR_SIZE);
        if (error != ESP_OK) { s_ready = false; return error; }
    }
    candidate.magic = META_MAGIC | (s_paused ? META_PAUSED : 0);
    candidate.generation = s_meta.generation + 1;
    candidate.crc = ~crc_extend(UINT32_MAX, &candidate, offsetof(journal_meta_t, crc));
    candidate.commit = COMMIT_MAGIC;
    error = esp_partition_write(s_partition, offset, &candidate, offsetof(journal_meta_t, commit));
    if (error == ESP_OK)
        error = esp_partition_write(s_partition, offset + offsetof(journal_meta_t, commit),
                                     &candidate.commit, sizeof(candidate.commit));
    if (error == ESP_OK) {
        error = esp_partition_read(s_partition, offset, &probe, sizeof(probe));
        if (error == ESP_OK && memcmp(&probe, &candidate, sizeof(candidate)))
            error = ESP_ERR_INVALID_CRC;
    }
    if (error != ESP_OK) { s_ready = false; return error; }
    s_meta = candidate;
    s_meta_offset = offset;
    return ESP_OK;
}

/* Persist the erase intent before touching data. Recovery can finish an
 * interrupted sector erase without mistaking its debris for an unacked record. */
static esp_err_t erase_sector(uint32_t sector)
{
    journal_meta_t candidate = s_meta;
    candidate.erase_sector = sector;
    esp_err_t error = save_meta(candidate);
    if (error != ESP_OK) return error;
    error = esp_partition_erase_range(s_partition,
               META_SECTORS * SECTOR_SIZE + (size_t)sector * SECTOR_SIZE, SECTOR_SIZE);
    if (error != ESP_OK) { s_ready = false; return error; }
    s_used -= s_sector_used[sector];
    s_sector_used[sector] = 0;
    s_sector_max[sector] = 0;
    update_watermark();
    candidate = s_meta;
    candidate.erase_sector = NO_SECTOR;
    error = save_meta(candidate);
    return error;
}

static esp_err_t reclaim_sector(uint32_t sector)
{
    journal_slot_t slot;
    for (uint32_t index = sector * SLOTS_PER_SECTOR;
         index < (sector + 1U) * SLOTS_PER_SECTOR; ++index) {
        esp_err_t error = read_slot(index, &slot);
        if (error != ESP_OK) return error;
        if (slot.commit == COMMIT_MAGIC) {
            if (!valid_slot(&slot)) return ESP_ERR_INVALID_CRC;
            if (slot.session == s_meta.session && slot.sequence > s_meta.ack)
                return ESP_ERR_NO_MEM;
        } else if (!partial_commit(slot.commit)) return ESP_ERR_INVALID_CRC;
    }
    return erase_sector(sector);
}

static esp_err_t read_sequence(uint32_t sequence, journal_slot_t *slot)
{
    if (!sequence || sequence <= s_meta.ack || sequence >= s_next)
        return ESP_ERR_NOT_FOUND;
    esp_err_t error = read_slot((sequence - 1U) % s_capacity, slot);
    if (error != ESP_OK) return error;
    if (!valid_slot(slot) || slot->session != s_meta.session || slot->sequence != sequence)
        return ESP_ERR_INVALID_CRC;
    return ESP_OK;
}

esp_err_t topology_journal_init(void)
{
    if (!s_mutex) s_mutex = xSemaphoreCreateMutex();
    if (!s_mutex) return ESP_ERR_NO_MEM;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    if (s_ready) { xSemaphoreGive(s_mutex); return ESP_OK; }
    esp_err_t error = ESP_OK;
    bool found = false, meta_blank = true;
    uint32_t damaged_generation = 0;
    journal_meta_t candidate;
    s_partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "topology");
    if (!s_partition || s_partition->size < 4U * SECTOR_SIZE || s_partition->size % SECTOR_SIZE) {
        error = ESP_ERR_NOT_FOUND;
        goto done;
    }
    s_sector_count = s_partition->size / SECTOR_SIZE - META_SECTORS;
    s_capacity = s_sector_count * SLOTS_PER_SECTOR;
    free(s_sector_used);
    free(s_sector_max);
    s_sector_used = calloc(s_sector_count, sizeof(*s_sector_used));
    s_sector_max = calloc(s_sector_count, sizeof(*s_sector_max));
    if (!s_sector_used || !s_sector_max) { error = ESP_ERR_NO_MEM; goto done; }
    s_used = 0;
    s_paused = false;
    memset(&s_meta, 0, sizeof(s_meta));
    for (uint32_t offset = 0; offset < META_SECTORS * SECTOR_SIZE; offset += sizeof(candidate)) {
        error = esp_partition_read(s_partition, offset, &candidate, sizeof(candidate));
        if (error != ESP_OK) goto done;
        if (!blank(&candidate, sizeof(candidate))) meta_blank = false;
        bool body_valid = (candidate.magic & ~META_PAUSED) == META_MAGIC &&
            candidate.crc == ~crc_extend(UINT32_MAX, &candidate, offsetof(journal_meta_t, crc)) &&
            memchr(candidate.owner, '\0', sizeof(candidate.owner)) &&
            (candidate.erase_sector == NO_SECTOR || candidate.erase_sector < s_sector_count);
        if (candidate.commit != COMMIT_MAGIC) {
            if (body_valid && !partial_commit(candidate.commit) && candidate.generation > damaged_generation)
                damaged_generation = candidate.generation;
            continue;
        }
        if (!body_valid) {
            error = ESP_ERR_INVALID_CRC;
            goto done;
        }
        if (!found || candidate.generation > s_meta.generation) {
            s_meta = candidate;
            s_meta_offset = offset;
            found = true;
        }
    }
    /* Retired banks contain older generations with zeroed commit words. A newer
     * such generation indicates damage, not retirement: reverting could replay
     * an obsolete erase intent against records written after its completion. */
    if (damaged_generation > s_meta.generation) { error = ESP_ERR_INVALID_CRC; goto done; }
    s_paused = found && (s_meta.magic & META_PAUSED) != 0;
    if (found && s_meta.erase_sector != NO_SECTOR) {
        error = esp_partition_erase_range(s_partition,
                 META_SECTORS * SECTOR_SIZE + (size_t)s_meta.erase_sector * SECTOR_SIZE, SECTOR_SIZE);
        if (error != ESP_OK) goto done;
        candidate = s_meta;
        candidate.erase_sector = NO_SECTOR;
        error = save_meta(candidate);
        if (error != ESP_OK) goto done;
    }
    s_next = s_meta.ack + 1U;
    journal_slot_t slot;
    for (uint32_t index = 0; index < s_capacity; ++index) {
        error = read_slot(index, &slot);
        if (error != ESP_OK) goto done;
        if (blank(&slot, sizeof(slot))) continue;
        ++s_used;
        ++s_sector_used[index / SLOTS_PER_SECTOR];
        if (!found) { error = ESP_ERR_INVALID_CRC; goto done; }
        if (slot.commit != COMMIT_MAGIC) {
            if (!partial_commit(slot.commit)) { error = ESP_ERR_INVALID_CRC; goto done; }
            continue;
        }
        if (!valid_slot(&slot)) { error = ESP_ERR_INVALID_CRC; goto done; }
        if (slot.session == s_meta.session && slot.sequence > s_meta.ack) {
            if ((slot.sequence - 1U) % s_capacity != index || slot.sequence == UINT32_MAX) {
                error = ESP_ERR_INVALID_CRC;
                goto done;
            }
            if (slot.sequence >= s_next) s_next = slot.sequence + 1U;
            if (slot.sequence > s_sector_max[index / SLOTS_PER_SECTOR])
                s_sector_max[index / SLOTS_PER_SECTOR] = slot.sequence;
        }
    }
    if (!found) {
        /* Only pristine data can finish an interrupted first metadata commit. */
        if (!meta_blank) {
            for (uint32_t bank = 0; bank < META_SECTORS; ++bank) {
                error = esp_partition_erase_range(s_partition, bank * SECTOR_SIZE, SECTOR_SIZE);
                if (error != ESP_OK) goto done;
            }
        }
        s_meta_offset = SECTOR_SIZE - sizeof(s_meta);
        candidate = s_meta;
        candidate.erase_sector = NO_SECTOR;
        error = save_meta(candidate);
        if (error != ESP_OK) goto done;
        s_next = 1;
    }
    if (s_next - s_meta.ack - 1U > s_capacity) { error = ESP_ERR_INVALID_CRC; goto done; }
    for (uint32_t sequence = s_meta.ack + 1U; sequence < s_next; ++sequence) {
        error = read_sequence(sequence, &slot);
        if (error != ESP_OK) goto done;
    }
    /* ACK commit may have reached flash immediately before power failed, before
     * reclamation started. Finish those complete sectors during recovery. */
    for (uint32_t sector = 0; sector < s_sector_count; ++sector) {
        if (s_sector_used[sector] == SLOTS_PER_SECTOR && s_sector_max[sector] <= s_meta.ack) {
            error = reclaim_sector(sector);
            if (error != ESP_OK) goto done;
        }
    }
    s_recovered = found && s_meta.session != 0;
    update_watermark();
    s_ready = true;
done:
    xSemaphoreGive(s_mutex);
    return error;
}

esp_err_t topology_journal_open(uint32_t session, const char *owner)
{
    if (!session || !valid_text(owner, TOPOLOGY_JOURNAL_OWNER_MAX, true)) return ESP_ERR_INVALID_ARG;
    if (!s_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t error = ESP_OK;
    if (!s_ready) error = ESP_ERR_INVALID_STATE;
    else if (session == s_meta.session) {
        if (strcmp(owner, s_meta.owner)) error = ESP_ERR_INVALID_STATE;
    } else if (s_meta.ack + 1U != s_next) error = ESP_ERR_INVALID_STATE;
    else {
        journal_meta_t candidate = s_meta;
        candidate.session = session;
        candidate.ack = candidate.ack_crc = 0;
        memset(candidate.owner, 0, sizeof(candidate.owner));
        memcpy(candidate.owner, owner, strlen(owner));
        error = save_meta(candidate);
        if (error == ESP_OK) {
            s_next = 1;
            s_recovered = false;
            for (uint32_t sector = 0; sector < s_sector_count && error == ESP_OK; ++sector)
                if (s_sector_used[sector]) error = reclaim_sector(sector);
        }
    }
    xSemaphoreGive(s_mutex);
    return error;
}

esp_err_t topology_journal_append(uint32_t job_id, const char *payload,
                                  uint32_t *sequence, uint32_t *crc)
{
    if (!sequence || !crc || !valid_text(payload, TOPOLOGY_JOURNAL_PAYLOAD_MAX, false))
        return ESP_ERR_INVALID_ARG;
    if (!s_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t error = ESP_OK;
    if (!s_ready || !s_meta.session || s_next == UINT32_MAX) {
        error = ESP_ERR_INVALID_STATE;
        goto done;
    }
    uint32_t index = (s_next - 1U) % s_capacity;
    journal_slot_t slot;
    error = read_slot(index, &slot);
    if (error != ESP_OK) goto done;
    if (!blank(&slot, sizeof(slot))) {
        error = reclaim_sector(index / SLOTS_PER_SECTOR);
        if (error != ESP_OK) goto done;
    }
    memset(&slot, 0xff, sizeof(slot));
    slot.magic = DATA_MAGIC;
    slot.session = s_meta.session;
    slot.sequence = s_next;
    slot.job_id = job_id;
    slot.length = (uint16_t)strlen(payload);
    slot.reserved = 0;
    memcpy(slot.payload, payload, slot.length + 1U);
    slot.crc = wire_crc(slot.session, slot.sequence, job_id, payload);
    slot.commit = COMMIT_MAGIC;
    error = esp_partition_write(s_partition, data_offset(index), &slot, offsetof(journal_slot_t, commit));
    if (error == ESP_OK)
        error = esp_partition_write(s_partition, data_offset(index) + offsetof(journal_slot_t, commit),
                                     &slot.commit, sizeof(slot.commit));
    if (error == ESP_OK) {
        journal_slot_t verified;
        error = read_slot(index, &verified);
        if (error == ESP_OK && memcmp(&verified, &slot, sizeof(slot))) error = ESP_ERR_INVALID_CRC;
    }
    if (error != ESP_OK) { s_ready = false; goto done; }
    ++s_used;
    ++s_sector_used[index / SLOTS_PER_SECTOR];
    s_sector_max[index / SLOTS_PER_SECTOR] = slot.sequence;
    ++s_next;
    *sequence = slot.sequence;
    *crc = slot.crc;
    bool was_paused = s_paused;
    update_watermark();
    if (was_paused != s_paused) error = save_meta(s_meta);
done:
    xSemaphoreGive(s_mutex);
    return error;
}

esp_err_t topology_journal_read(uint32_t sequence, topology_journal_record_t *record)
{
    if (!record) return ESP_ERR_INVALID_ARG;
    if (!s_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    journal_slot_t slot;
    esp_err_t error = s_ready ? read_sequence(sequence, &slot) : ESP_ERR_INVALID_STATE;
    if (error == ESP_OK) {
        record->session = slot.session;
        record->sequence = slot.sequence;
        record->job_id = slot.job_id;
        record->crc = slot.crc;
        memcpy(record->payload, slot.payload, slot.length + 1U);
    }
    xSemaphoreGive(s_mutex);
    return error;
}

esp_err_t topology_journal_ack(uint32_t sequence, uint32_t crc)
{
    if (!s_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t error = ESP_OK;
    if (!s_ready || !s_meta.session) { error = ESP_ERR_INVALID_STATE; goto done; }
    if (sequence == s_meta.ack) {
        if (crc != s_meta.ack_crc) error = ESP_ERR_INVALID_CRC;
        goto done;
    }
    journal_slot_t slot;
    error = read_sequence(sequence, &slot);
    if (error != ESP_OK) goto done;
    if (slot.crc != crc) { error = ESP_ERR_INVALID_CRC; goto done; }
    journal_meta_t candidate = s_meta;
    candidate.ack = sequence;
    candidate.ack_crc = crc;
    error = save_meta(candidate);
    if (error != ESP_OK) goto done;
    /* Retain a partially filled tail sector to avoid erasing it on every ACK. */
    for (uint32_t sector = 0; sector < s_sector_count; ++sector) {
        if (s_sector_used[sector] != SLOTS_PER_SECTOR || s_sector_max[sector] > s_meta.ack) continue;
        error = reclaim_sector(sector);
        if (error == ESP_ERR_NO_MEM) { error = ESP_OK; continue; }
        if (error != ESP_OK) break;
    }
done:
    xSemaphoreGive(s_mutex);
    return error;
}

esp_err_t topology_journal_get_info(topology_journal_info_t *info)
{
    if (!info) return ESP_ERR_INVALID_ARG;
    if (!s_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t error = s_ready ? ESP_OK : ESP_ERR_INVALID_STATE;
    if (error == ESP_OK) {
        memset(info, 0, sizeof(*info));
        info->session = s_meta.session;
        info->ack = s_meta.ack;
        info->ack_crc = s_meta.ack_crc;
        info->next = s_next;
        info->first = s_meta.ack + 1U;
        info->used_records = s_used;
        info->capacity_records = s_capacity;
        memcpy(info->owner, s_meta.owner, sizeof(info->owner));
        info->recovered = s_recovered;
    }
    xSemaphoreGive(s_mutex);
    return error;
}

bool topology_journal_should_pause(void)
{
    if (!s_mutex) return true;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    bool paused = !s_ready || s_paused;
    xSemaphoreGive(s_mutex);
    return paused;
}
