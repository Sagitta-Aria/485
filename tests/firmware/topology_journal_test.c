/* Exercise the production persistence code on NOR flash with byte-precise cuts. */
#include <stdio.h>
#include <string.h>
#include "../../master/main/topology_journal.c"

#define CHECK(condition) do { if (!(condition)) return __LINE__; } while (0)
#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

static uint8_t flash[6 * SECTOR_SIZE];
static const esp_partition_t partition = { sizeof(flash) };
static int write_budget = -1, erase_budget = -1;
static int silent_write_call = -1;
static unsigned nor_errors, writes, erases;
struct fake_mutex { bool locked; };
static struct fake_mutex mutex;

SemaphoreHandle_t xSemaphoreCreateMutex(void) { return &mutex; }
BaseType_t xSemaphoreTake(SemaphoreHandle_t handle, TickType_t wait)
{
    (void)wait;
    if (handle->locked) ++nor_errors;
    handle->locked = true;
    return pdTRUE;
}
void xSemaphoreGive(SemaphoreHandle_t handle) { handle->locked = false; }

const esp_partition_t *esp_partition_find_first(int type, int subtype, const char *label)
{
    return type == ESP_PARTITION_TYPE_DATA && subtype == 0x40 && !strcmp(label, "topology") ? &partition : NULL;
}
esp_err_t esp_partition_read(const esp_partition_t *part, size_t offset, void *dest, size_t size)
{
    if (part != &partition || offset + size > sizeof(flash)) return ESP_FAIL;
    memcpy(dest, flash + offset, size);
    return ESP_OK;
}
esp_err_t esp_partition_write(const esp_partition_t *part, size_t offset, const void *src, size_t size)
{
    if (part != &partition || offset + size > sizeof(flash)) return ESP_FAIL;
    ++writes;
    if ((int)writes == silent_write_call) return ESP_OK;
    const uint8_t *bytes = src;
    for (size_t index = 0; index < size; ++index) {
        if (write_budget == 0) return ESP_FAIL;
        if (write_budget > 0) --write_budget;
        if ((flash[offset + index] & bytes[index]) != bytes[index]) { ++nor_errors; return ESP_FAIL; }
        flash[offset + index] &= bytes[index];
    }
    return ESP_OK;
}
esp_err_t esp_partition_erase_range(const esp_partition_t *part, size_t offset, size_t size)
{
    if (part != &partition || offset % SECTOR_SIZE || size % SECTOR_SIZE || offset + size > sizeof(flash)) {
        ++nor_errors;
        return ESP_FAIL;
    }
    ++erases;
    for (size_t index = 0; index < size; ++index) {
        if (erase_budget == 0) return ESP_FAIL;
        if (erase_budget > 0) --erase_budget;
        flash[offset + index] = 0xff;
    }
    return ESP_OK;
}

static esp_err_t reboot(void)
{
    s_ready = false;
    write_budget = erase_budget = -1;
    silent_write_call = -1;
    return topology_journal_init();
}
static esp_err_t fresh(void)
{
    memset(flash, 0xff, sizeof(flash));
    nor_errors = writes = erases = 0;
    esp_err_t error = reboot();
    return error == ESP_OK ? topology_journal_open(1234, "pc") : error;
}
static esp_err_t append(uint32_t *sequence, uint32_t *crc)
{
    return topology_journal_append(17, "TOPO_DATA 1234 17 0 2 25380", sequence, crc);
}

EXPORT int journal_test_ack_replay_and_crc(void)
{
    CHECK(fresh() == ESP_OK);
    uint32_t sequence, crc;
    CHECK(append(&sequence, &crc) == ESP_OK && sequence == 1);
    /* Python independently checks this production wire CRC against zlib. */
    CHECK(crc == UINT32_C(0x3c2a7b92));
    topology_journal_record_t record;
    CHECK(reboot() == ESP_OK);
    CHECK(topology_journal_read(1, &record) == ESP_OK && record.crc == crc);
    CHECK(topology_journal_ack(1, crc ^ 1) == ESP_ERR_INVALID_CRC);
    CHECK(topology_journal_ack(2, crc) != ESP_OK);
    CHECK(topology_journal_open(1235, "pc") == ESP_ERR_INVALID_STATE);
    CHECK(topology_journal_open(1234, "other") == ESP_ERR_INVALID_STATE);
    CHECK(topology_journal_ack(1, crc) == ESP_OK);
    CHECK(reboot() == ESP_OK && topology_journal_ack(1, crc) == ESP_OK);
    CHECK(topology_journal_read(1, &record) == ESP_ERR_NOT_FOUND);
    topology_journal_info_t info;
    CHECK(topology_journal_get_info(&info) == ESP_OK && info.recovered && info.ack == 1);
    CHECK(topology_journal_open(1235, "pc") == ESP_OK);
    CHECK(append(&sequence, &crc) == ESP_OK && sequence == 1);
    CHECK(!nor_errors);
    return 0;
}

EXPORT int journal_test_append_power_cuts(void)
{
    for (int cut = 0; cut < 256; ++cut) {
        CHECK(fresh() == ESP_OK);
        uint32_t first, crc, sequence, next_crc;
        CHECK(append(&first, &crc) == ESP_OK);
        write_budget = cut;
        CHECK(append(&sequence, &next_crc) != ESP_OK);
        CHECK(reboot() == ESP_OK);
        topology_journal_record_t record;
        CHECK(topology_journal_read(1, &record) == ESP_OK && record.crc == crc);
        CHECK(topology_journal_read(2, &record) == ESP_ERR_NOT_FOUND);
        CHECK(topology_journal_ack(1, crc) == ESP_OK);
        CHECK(append(&sequence, &next_crc) == ESP_OK && sequence == 2);
        CHECK(reboot() == ESP_OK && topology_journal_read(2, &record) == ESP_OK);
        CHECK(!nor_errors);
    }
    return 0;
}

EXPORT int journal_test_ack_power_cuts(void)
{
    for (int cut = 0; cut < 64; ++cut) {
        CHECK(fresh() == ESP_OK);
        uint32_t sequence, crc;
        CHECK(append(&sequence, &crc) == ESP_OK);
        write_budget = cut;
        CHECK(topology_journal_ack(sequence, crc) != ESP_OK);
        CHECK(reboot() == ESP_OK);
        topology_journal_record_t record;
        CHECK(topology_journal_read(sequence, &record) == ESP_OK && record.crc == crc);
        CHECK(topology_journal_ack(sequence, crc) == ESP_OK);
        CHECK(reboot() == ESP_OK && topology_journal_ack(sequence, crc) == ESP_OK);
        CHECK(!nor_errors);
    }
    return 0;
}

EXPORT int journal_test_watermark_and_ring(void)
{
    CHECK(fresh() == ESP_OK);
    uint32_t sequence, crc, crcs[160];
    for (unsigned count = 0; count < 64; ++count) {
        CHECK(append(&sequence, &crc) == ESP_OK);
        crcs[count] = crc;
        CHECK(topology_journal_should_pause() == (count >= 44));
    }
    CHECK(append(&sequence, &crc) == ESP_ERR_NO_MEM);
    CHECK(topology_journal_ack(16, crcs[15]) == ESP_OK && topology_journal_should_pause());
    CHECK(topology_journal_ack(32, crcs[31]) == ESP_OK && topology_journal_should_pause());
    CHECK(reboot() == ESP_OK && topology_journal_should_pause());
    CHECK(topology_journal_ack(48, crcs[47]) == ESP_OK && !topology_journal_should_pause());
    for (unsigned count = 64; count < 160; ++count) {
        CHECK(append(&sequence, &crc) == ESP_OK && sequence == count + 1);
        crcs[count] = crc;
        CHECK(topology_journal_ack(sequence, crc) == ESP_OK);
        CHECK(reboot() == ESP_OK);
    }
    CHECK(topology_journal_ack(160, crcs[159]) == ESP_OK && !nor_errors);
    return 0;
}

EXPORT int journal_test_corruption_and_erase_recovery(void)
{
    CHECK(fresh() == ESP_OK);
    uint32_t sequence, crc;
    CHECK(append(&sequence, &crc) == ESP_OK);
    flash[data_offset(0) + offsetof(journal_slot_t, payload)] ^= 1;
    CHECK(reboot() == ESP_ERR_INVALID_CRC);
    for (unsigned bit = 0; bit < 32; ++bit) {
        if (!(COMMIT_MAGIC & (UINT32_C(1) << bit))) continue;
        CHECK(fresh() == ESP_OK);
        CHECK(append(&sequence, &crc) == ESP_OK);
        size_t offset = data_offset(0) + offsetof(journal_slot_t, commit) + bit / 8;
        flash[offset] &= (uint8_t)~(1U << (bit % 8));
        unsigned before = erases;
        CHECK(reclaim_sector(0) == ESP_ERR_INVALID_CRC && erases == before);
        CHECK(reboot() == ESP_ERR_INVALID_CRC && erases == before);
        CHECK(topology_journal_open(5678, "pc") == ESP_ERR_INVALID_STATE && erases == before);
    }
    for (int cut = 0; cut < 4096; cut += 127) {
        CHECK(fresh() == ESP_OK);
        for (unsigned index = 0; index < 16; ++index) CHECK(append(&sequence, &crc) == ESP_OK);
        erase_budget = cut;
        CHECK(topology_journal_ack(sequence, crc) != ESP_OK);
        CHECK(reboot() == ESP_OK && topology_journal_ack(sequence, crc) == ESP_OK);
        CHECK(append(&sequence, &crc) == ESP_OK && sequence == 17);
        CHECK(!nor_errors);
    }
    return 0;
}

EXPORT int journal_test_metadata_bank_power_cuts(void)
{
    for (unsigned bit = 0; bit < 32; ++bit) {
        if (!(COMMIT_MAGIC & (UINT32_C(1) << bit))) continue;
        CHECK(fresh() == ESP_OK);
        uint32_t sequence, crc;
        CHECK(append(&sequence, &crc) == ESP_OK);
        CHECK(topology_journal_ack(sequence, crc) == ESP_OK);
        CHECK(topology_journal_open(5678, "pc") == ESP_OK);
        CHECK(append(&sequence, &crc) == ESP_OK);
        size_t offset = s_meta_offset + offsetof(journal_meta_t, commit) + bit / 8;
        flash[offset] &= (uint8_t)~(1U << (bit % 8));
        unsigned before = erases;
        CHECK(reboot() == ESP_ERR_INVALID_CRC && erases == before);
        journal_slot_t slot;
        CHECK(read_slot(0, &slot) == ESP_OK && valid_slot(&slot) && slot.session == 5678);
        CHECK(topology_journal_open(9999, "pc") == ESP_ERR_INVALID_STATE && erases == before);
    }
    for (int cut = 0; cut < 4096; cut += 127) {
        CHECK(fresh() == ESP_OK);
        journal_meta_t candidate = s_meta;
        while ((s_meta_offset % SECTOR_SIZE) < SECTOR_SIZE - sizeof(candidate))
            CHECK(save_meta(candidate) == ESP_OK);
        uint32_t sequence, crc;
        CHECK(append(&sequence, &crc) == ESP_OK);
        erase_budget = cut;
        CHECK(topology_journal_ack(sequence, crc) != ESP_OK);
        CHECK(reboot() == ESP_OK);
        topology_journal_record_t record;
        CHECK(topology_journal_read(sequence, &record) == ESP_OK);
        CHECK(topology_journal_ack(sequence, crc) == ESP_OK);
        CHECK(!nor_errors);
    }
    for (int cut = 0; cut < 320; ++cut) {
        CHECK(fresh() == ESP_OK);
        journal_meta_t candidate = s_meta;
        while ((s_meta_offset % SECTOR_SIZE) < SECTOR_SIZE - sizeof(candidate))
            CHECK(save_meta(candidate) == ESP_OK);
        uint32_t sequence, crc;
        CHECK(append(&sequence, &crc) == ESP_OK);
        write_budget = cut;
        CHECK(topology_journal_ack(sequence, crc) != ESP_OK);
        CHECK(reboot() == ESP_OK);
        CHECK(topology_journal_ack(sequence, crc) == ESP_OK && !nor_errors);
    }
    for (int cut = 0; cut < 192; ++cut) {
        CHECK(fresh() == ESP_OK);
        uint32_t sequence, crc;
        for (unsigned index = 0; index < 16; ++index) CHECK(append(&sequence, &crc) == ESP_OK);
        write_budget = cut;
        CHECK(topology_journal_ack(sequence, crc) != ESP_OK);
        CHECK(reboot() == ESP_OK);
        CHECK(topology_journal_ack(sequence, crc) == ESP_OK && !nor_errors);
        topology_journal_info_t info;
        CHECK(topology_journal_get_info(&info) == ESP_OK && info.used_records == 0);
    }
    return 0;
}

EXPORT int journal_test_silent_program_failure(void)
{
    for (int lost = 1; lost <= 2; ++lost) {
        CHECK(fresh() == ESP_OK);
        uint32_t sequence, crc;
        CHECK(append(&sequence, &crc) == ESP_OK);
        uint32_t original_crc = crc;
        silent_write_call = (int)writes + lost;
        CHECK(append(&sequence, &crc) == ESP_ERR_INVALID_CRC);
        CHECK(!s_ready && s_next == 2);
        CHECK(append(&sequence, &crc) == ESP_ERR_INVALID_STATE);
        CHECK(topology_journal_ack(1, original_crc) == ESP_ERR_INVALID_STATE);
        journal_slot_t slot;
        CHECK(read_slot(0, &slot) == ESP_OK && valid_slot(&slot) && slot.crc == original_crc);
    }
    for (int lost = 1; lost <= 2; ++lost) {
        CHECK(fresh() == ESP_OK);
        uint32_t sequence, crc;
        for (unsigned index = 0; index < 16; ++index) CHECK(append(&sequence, &crc) == ESP_OK);
        unsigned before = erases;
        silent_write_call = (int)writes + lost;
        CHECK(topology_journal_ack(sequence, crc) == ESP_ERR_INVALID_CRC);
        CHECK(!s_ready && s_meta.ack == 0 && erases == before);
        CHECK(append(&sequence, &crc) == ESP_ERR_INVALID_STATE);
    }
    CHECK(fresh() == ESP_OK);
    uint32_t sequence, crc;
    CHECK(append(&sequence, &crc) == ESP_OK);
    CHECK(topology_journal_ack(sequence, crc) == ESP_OK);
    /* The new session, erase intent, and erase completion each use two writes.
     * Losing the final commit must prevent reuse until recovery finishes it. */
    silent_write_call = (int)writes + 6;
    CHECK(topology_journal_open(5678, "pc") == ESP_ERR_INVALID_CRC);
    CHECK(!s_ready && s_meta.erase_sector == 0);
    CHECK(append(&sequence, &crc) == ESP_ERR_INVALID_STATE);
    CHECK(reboot() == ESP_OK && s_meta.erase_sector == NO_SECTOR);
    CHECK(topology_journal_open(5678, "pc") == ESP_OK);
    CHECK(append(&sequence, &crc) == ESP_OK && sequence == 1);
    CHECK(reboot() == ESP_OK);
    topology_journal_record_t record;
    CHECK(topology_journal_read(1, &record) == ESP_OK && record.session == 5678);
    CHECK(!nor_errors);
    return 0;
}
