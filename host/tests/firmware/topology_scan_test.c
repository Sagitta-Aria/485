/* Execute the actual master state machine against deterministic fake devices.
 * These tests exercise protocol/state ordering, not FreeRTOS scheduling or GPIO.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../../master/main/topology_journal.h"
#include "../../../master/main/topology_scan.c"

#define CHECK(condition) do { if (!(condition)) return __LINE__; } while (0)
#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

struct fake_queue {
    unsigned capacity, item_size, count;
    unsigned char data[8][320];
};
struct fake_mutex { bool locked; };
static struct fake_queue queues[4];
static unsigned queue_count;
static struct fake_mutex mutex;
static TaskFunction_t pending_scan;
static int64_t fake_time;
static bool fixed_route, peer_fails, fail_cleanup, inject_stale;
static unsigned measurements, active_masks, remote_clears, reset_calls;
static unsigned positive_closures, cancellation_after;
static int active_source;
static bool peer_prepared;
static uint32_t remote_masks[10];
static char last_reply[TEXT_SIZE];
static unsigned last_reset_count;
static unsigned online_modules;
static unsigned lock_errors;
static topology_journal_info_t journal_info;
static topology_journal_record_t journal_records[256];
static bool journal_paused;
static void (*delay_hook)(void);
static unsigned disconnect_during_measurement;
static unsigned invalidate_peer_during_measurement;
static unsigned disconnect_after_commit;
static unsigned pause_after_commit;
static bool result_transport_fails;
static unsigned reply_count;
static unsigned scan_tasks_created;
static uint32_t prepared_peer_token;
static char replies[128][TEXT_SIZE];

esp_err_t topology_journal_init(void) { return ESP_OK; }
esp_err_t topology_journal_open(uint32_t session, const char *owner)
{
    if (!session || !owner || !*owner) return ESP_ERR_INVALID_ARG;
    if (journal_info.session == session)
        return strcmp(journal_info.owner, owner) ? ESP_ERR_INVALID_STATE : ESP_OK;
    if (journal_info.used_records) return ESP_ERR_INVALID_STATE;
    memset(&journal_info, 0, sizeof(journal_info));
    memset(journal_records, 0, sizeof(journal_records));
    journal_info.session = session;
    journal_info.first = journal_info.next = 1;
    journal_info.capacity_records = 256;
    snprintf(journal_info.owner, sizeof(journal_info.owner), "%s", owner);
    return ESP_OK;
}
esp_err_t topology_journal_append(uint32_t job, const char *payload,
                                  uint32_t *sequence, uint32_t *crc)
{
    if (!journal_info.session || journal_info.next > 256) return ESP_ERR_INVALID_STATE;
    if (strlen(payload) > TOPOLOGY_JOURNAL_PAYLOAD_MAX) return ESP_ERR_INVALID_SIZE;
    topology_journal_record_t *record = &journal_records[journal_info.next - 1];
    *record = (topology_journal_record_t){.session = journal_info.session,
        .sequence = journal_info.next, .job_id = job, .crc = 0xc0000000U ^ journal_info.next ^ job};
    snprintf(record->payload, sizeof(record->payload), "%s", payload);
    *sequence = record->sequence; *crc = record->crc;
    ++journal_info.next;
    ++journal_info.used_records;
    if (disconnect_after_commit && *sequence == disconnect_after_commit)
        topology_scan_disconnected();
    if (pause_after_commit && *sequence == pause_after_commit) journal_paused = true;
    return ESP_OK;
}
esp_err_t topology_journal_read(uint32_t sequence, topology_journal_record_t *record)
{
    if (sequence < journal_info.first || sequence >= journal_info.next) return ESP_ERR_INVALID_ARG;
    *record = journal_records[sequence - 1];
    return ESP_OK;
}
esp_err_t topology_journal_ack(uint32_t sequence, uint32_t crc)
{
    if (sequence == journal_info.ack)
        return crc == journal_info.ack_crc ? ESP_OK : ESP_ERR_INVALID_ARG;
    if (sequence <= journal_info.ack || sequence >= journal_info.next ||
        journal_records[sequence - 1].crc != crc) return ESP_ERR_INVALID_ARG;
    journal_info.used_records -= sequence - journal_info.ack;
    journal_info.ack = sequence;
    journal_info.ack_crc = crc;
    journal_info.first = sequence + 1;
    return ESP_OK;
}
esp_err_t topology_journal_get_info(topology_journal_info_t *info)
{
    *info = journal_info;
    return ESP_OK;
}
bool topology_journal_should_pause(void) { return journal_paused; }

QueueHandle_t xQueueCreate(unsigned capacity, unsigned item_size)
{
    if (queue_count >= 4 || capacity > 8 || item_size > 320) return NULL;
    struct fake_queue *queue = &queues[queue_count++];
    memset(queue, 0, sizeof(*queue));
    queue->capacity = capacity;
    queue->item_size = item_size;
    return queue;
}

BaseType_t xQueueSend(QueueHandle_t queue, const void *item, TickType_t wait)
{
    (void)wait;
    if (queue->count >= queue->capacity) return pdFALSE;
    memcpy(queue->data[queue->count++], item, queue->item_size);
    return pdTRUE;
}

BaseType_t xQueueReceive(QueueHandle_t queue, void *item, TickType_t wait)
{
    if (!queue->count) { fake_time += (int64_t)wait * 1000; return pdFALSE; }
    memcpy(item, queue->data[0], queue->item_size);
    --queue->count;
    for (unsigned index = 0; index < queue->count; ++index)
        memcpy(queue->data[index], queue->data[index + 1], queue->item_size);
    return pdTRUE;
}

void xQueueReset(QueueHandle_t queue) { queue->count = 0; }
SemaphoreHandle_t xSemaphoreCreateMutex(void) { mutex.locked = false; return &mutex; }
BaseType_t xSemaphoreTake(SemaphoreHandle_t semaphore, TickType_t wait)
{
    if (semaphore->locked) { if (wait) ++lock_errors; return pdFALSE; }
    semaphore->locked = true;
    return pdTRUE;
}
void xSemaphoreGive(SemaphoreHandle_t semaphore) { semaphore->locked = false; }
BaseType_t xTaskCreate(TaskFunction_t function, const char *name, unsigned stack,
                       void *argument, unsigned priority, void *handle)
{
    (void)stack; (void)argument; (void)priority; (void)handle;
    if (!strncmp(name, "topo_scan", 9) || !strcmp(name, "topo_cached")) {
        pending_scan = function;
        ++scan_tasks_created;
    }
    return pdPASS;
}
void vTaskDelay(TickType_t ticks)
{
    fake_time += (int64_t)ticks * 1000;
    if (delay_hook) delay_hook();
}
void vTaskDelete(void *task) { (void)task; }
int64_t esp_timer_get_time(void) { return fake_time; }
const char *esp_err_to_name(esp_err_t error)
{
    if (error == ESP_OK) return "ESP_OK";
    return error == ESP_ERR_TIMEOUT ? "ESP_ERR_TIMEOUT" : "FAKE_ERROR";
}
bool ch446_fixed_kelvin_enabled(void) { return fixed_route; }
esp_err_t ch446_reset_all(void) { return ESP_OK; }
/* Model a contiguous physical bus; absent addresses time out during discovery. */
esp_err_t rs485_master_probe_module(unsigned module)
{
    if (module >= 10) return ESP_ERR_INVALID_ARG;
    return module < online_modules ? ESP_OK : ESP_ERR_TIMEOUT;
}
esp_err_t rs485_master_reset_modules(unsigned count)
{
    ++reset_calls;
    last_reset_count = count;
    unsigned reachable_count = count < online_modules ? count : online_modules;
    if (active_source >= 0 && (unsigned)active_source / 24 < reachable_count) active_source = -1;
    active_masks = 0;
    for (unsigned module = 0; module < reachable_count; ++module) remote_masks[module] = 0;
    if (count > online_modules) return ESP_ERR_TIMEOUT;
    return fail_cleanup && reset_calls > 1 ? ESP_FAIL : ESP_OK;
}
esp_err_t rs485_master_apply_mask(unsigned module, uint32_t session,
                                uint32_t step, uint32_t mask, bool positive)
{
    (void)session; (void)step;
    if (module >= 10 || mask > 0xFFFFFF) return ESP_ERR_INVALID_ARG;
    if (module >= online_modules) return ESP_ERR_TIMEOUT;
    if (positive && mask) {
        if (!peer_prepared || active_source >= 0) return ESP_ERR_INVALID_STATE;
        unsigned index = 0;
        while (!(mask & (1U << index))) ++index;
        if (mask != (1U << index)) return ESP_ERR_INVALID_ARG;
        active_source = (int)(module * 24 + index);
        ++positive_closures;
    } else if (positive && active_source / 24 == (int)module) active_source = -1;
    if (!positive) remote_masks[module] = mask;
    if (mask) ++active_masks;
    return ESP_OK;
}
esp_err_t xd31h_read_measurement(xd31h_measurement_t *value, uint32_t timeout)
{
    (void)timeout;
    if (active_source < 0 || !peer_prepared) return ESP_ERR_INVALID_STATE;
    *value = (xd31h_measurement_t){.valid = true, .range = 2, .raw_value = 100, .resistance_ohm = 0.1f};
    ++measurements;
    if (cancellation_after && measurements == cancellation_after) s_cancel = true;
    if (disconnect_during_measurement && measurements == disconnect_during_measurement)
        topology_scan_disconnected();
    if (invalidate_peer_during_measurement && measurements == invalidate_peer_during_measurement)
        prepared_peer_token = 0;
    return ESP_OK;
}
void xd31h_format_diagnostic(const xd31h_measurement_t *value, esp_err_t error,
                             char *buffer, size_t size)
{
    (void)value; (void)error;
    snprintf(buffer, size, "TIMEOUT received=0");
}
esp_err_t wifi_server_send_result(const char *destination, const char *request,
                                 const char *payload)
{
    (void)destination; (void)request;
    if (result_transport_fails) return ESP_FAIL;
    snprintf(last_reply, sizeof(last_reply), "%s", payload);
    if (reply_count < 128) snprintf(replies[reply_count++], TEXT_SIZE, "%s", payload);
    return ESP_OK;
}
esp_err_t wifi_server_send_request(const char *target, const char *request,
                                   const char *payload)
{
    char reply[TEXT_SIZE];
    if (!strncmp(payload, "TOPO_CLEAR ", 11)) {
        ++remote_clears;
        peer_prepared = false;
        prepared_peer_token = 0;
        snprintf(reply, sizeof(reply), "OK TOPO_CLEAR");
    } else if (!strncmp(payload, "TOPO_PREP2 ", 11) || !strncmp(payload, "TOPO_PICK2 ", 11)) {
        unsigned token;
        if (sscanf(payload, "%*s %*u %*u %u", &token) != 1) return ESP_ERR_INVALID_ARG;
        peer_prepared = !peer_fails;
        prepared_peer_token = token;
        if (peer_fails) snprintf(reply, sizeof(reply), "ERR TOPO_PREP2 FAKE_FAILURE");
        else snprintf(reply, sizeof(reply), "OK %.10s %u", payload, token);
    } else if (!strncmp(payload, "TOPO_SPAN2 ", 11)) {
        unsigned first, end, token;
        if (sscanf(payload, "%*s %*u %u %u %u", &first, &end, &token) != 3 || first >= end || end > 240)
            return ESP_ERR_INVALID_ARG;
        peer_prepared = !peer_fails;
        prepared_peer_token = token;
        snprintf(reply, sizeof(reply), peer_fails ? "ERR TOPO_SPAN2 FAKE_FAILURE" : "OK TOPO_SPAN2 %u", token);
    } else if (!strncmp(payload, "TOPO_VALIDATE ", 14)) {
        unsigned token;
        if (sscanf(payload, "%*s %*u %u", &token) != 1) return ESP_ERR_INVALID_ARG;
        snprintf(reply, sizeof(reply), "%s", peer_prepared && prepared_peer_token == token ?
            "OK TOPO_VALIDATE" : "ERR TOPO_VALIDATE SELECTION_CHANGED");
    } else if (!strncmp(payload, "TOPO_PREP ", 10) || !strncmp(payload, "TOPO_PICK ", 10)) {
        peer_prepared = !peer_fails;
        snprintf(reply, sizeof(reply), peer_fails ? "ERR TOPO_PREP FAKE_FAILURE" : "OK %.9s", payload);
    } else return ESP_ERR_INVALID_ARG;
    if (inject_stale) topology_scan_feed_result("stale-peer", WIFI_DEVICE_ID, request, "ERR STALE");
    topology_scan_feed_result(target, WIFI_DEVICE_ID, request, reply);
    return ESP_OK;
}

static void reset_test(bool source)
{
    memset(&s_plan, 0, sizeof(s_plan));
    memset(&s_job, 0, sizeof(s_job));
    s_lock = NULL; s_commands = NULL; s_peer_replies = NULL;
    s_epoch = 0; s_cancel = false;
    s_sequence = 0; s_bus_step = 0;
    s_selected_modules = TOPOLOGY_DEFAULT_MODULES;
    s_journal_ready = false; s_cache_session = 0; s_cache_owner[0] = '\0';
    s_cache_recovered = false;
    s_wait_peer[0] = s_wait_request[0] = '\0';
    queue_count = 0; pending_scan = NULL; fake_time = 1000;
    fixed_route = source; peer_fails = false; fail_cleanup = false; inject_stale = false;
    measurements = active_masks = remote_clears = reset_calls = positive_closures = cancellation_after = 0;
    last_reset_count = lock_errors = 0;
    online_modules = 10;
    memset(&journal_info, 0, sizeof(journal_info));
    memset(journal_records, 0, sizeof(journal_records));
    journal_info.first = journal_info.next = 1;
    journal_info.capacity_records = 256;
    journal_paused = false; delay_hook = NULL;
    disconnect_during_measurement = disconnect_after_commit = pause_after_commit = reply_count = scan_tasks_created = 0;
    invalidate_peer_during_measurement = prepared_peer_token = 0;
    result_transport_fails = false;
    memset(replies, 0, sizeof(replies));
    active_source = -1; peer_prepared = false; last_reply[0] = '\0';
    memset(remote_masks, 0, sizeof(remote_masks));
    topology_scan_init();
}

static void command(const char *source, const char *text)
{
    command_t entry = {.epoch = epoch()};
    snprintf(entry.source, sizeof(entry.source), "%s", source);
    snprintf(entry.request, sizeof(entry.request), "TEST-1");
    snprintf(entry.text, sizeof(entry.text), "%s", text);
    execute(&entry);
}

EXPORT int topology_test_remote_plan(void)
{
    reset_test(false);
    command("GUI", "TOPO_BEGIN 123 2 2"); CHECK(!strcmp(last_reply, "OK TOPO_BEGIN"));
    command("GUI", "TOPO_MASK 123 0 0 000001"); CHECK(!strcmp(last_reply, "OK TOPO_MASK"));
    command("GUI", "TOPO_SEAL 123"); CHECK(strstr(last_reply, "PLAN_INCOMPLETE"));
    command("GUI", "TOPO_MASK 123 0 1 000002");
    command("GUI", "TOPO_MASK 123 1 0 000004");
    command("GUI", "TOPO_MASK 123 1 1 000000");
    command("GUI", "TOPO_SEAL 123"); CHECK(!strcmp(last_reply, "OK TOPO_SEAL"));
    command("peer", "TOPO_PREP 123 0"); CHECK(!strcmp(last_reply, "OK TOPO_PREP"));
    CHECK(remote_masks[0] == 1 && remote_masks[1] == 2);
    command("stranger", "TOPO_PREP 123 1"); CHECK(strncmp(last_reply, "ERR ", 4) == 0);
    command("peer", "TOPO_PICK 123 25"); CHECK(!strcmp(last_reply, "OK TOPO_PICK"));
    CHECK(remote_masks[0] == 0 && remote_masks[1] == 2);
    CHECK(topology_scan_debug_lock() != ESP_OK);
    command("GUI", "TOPO_RESET 124"); CHECK(strncmp(last_reply, "ERR ", 4) == 0);
    command("GUI", "TOPO_RESET 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESET"));
    CHECK(s_plan.session == 0 && last_reset_count == 2 && lock_errors == 0);
    return 0;
}

EXPORT int topology_test_source_scan(void)
{
    reset_test(true);
    inject_stale = true;
    command("GUI", "TOPO_DISCOVER 1");
    command("GUI", "TOPO_RUN 123 peer 1 2 0");
    CHECK(!strcmp(last_reply, "OK TOPO_RUN") && pending_scan);
    CHECK(topology_scan_debug_lock() != ESP_OK);
    pending_scan(NULL);
    CHECK(!strcmp(last_reply, "TOPO_DONE 123 48"));
    CHECK(measurements == 48 && positive_closures == 48 && active_source == -1);
    CHECK(remote_clears == 1 && last_reset_count == 1 && lock_errors == 0);
    command("GUI", "TOPO_RESET 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESET"));
    return 0;
}

EXPORT int topology_test_cancel_and_owner(void)
{
    reset_test(true);
    command("GUI", "TOPO_RUN 123 peer 1 2 0"); CHECK(pending_scan);
    command("GUI", "TOPO_RESET 123"); CHECK(strstr(last_reply, "BUSY_USE_ABORT"));
    command("stranger", "TOPO_ABORT 123"); CHECK(strstr(last_reply, "SESSION_MISMATCH"));
    cancellation_after = 3;
    pending_scan(NULL);
    CHECK(!strcmp(last_reply, "TOPO_STOPPED 123 3"));
    CHECK(measurements == 3 && active_source == -1 && remote_clears == 1);
    CHECK(lock_errors == 0);
    return 0;
}

EXPORT int topology_test_failed_ready(void)
{
    reset_test(true);
    peer_fails = true;
    command("GUI", "TOPO_RUN 123 peer 1 2 0"); CHECK(pending_scan);
    pending_scan(NULL);
    CHECK(strncmp(last_reply, "TOPO_FAILED 123 ", 16) == 0);
    CHECK(measurements == 0 && positive_closures == 0 && remote_clears == 1);
    CHECK(active_source == -1 && lock_errors == 0);
    return 0;
}

EXPORT int topology_test_cleanup_error(void)
{
    reset_test(true);
    fail_cleanup = true;
    command("GUI", "TOPO_RUN 123 peer 1 1 0"); CHECK(pending_scan);
    pending_scan(NULL);
    CHECK(strncmp(last_reply, "TOPO_FAILED 123 ", 16) == 0);
    CHECK(measurements == 24 && remote_clears == 1 && active_source == -1);
    CHECK(lock_errors == 0);
    return 0;
}

EXPORT int topology_test_role_and_arguments(void)
{
    reset_test(false);
    command("GUI", "TOPO_RUN 123 peer 1 1 0"); CHECK(strncmp(last_reply, "ERR ", 4) == 0);
    CHECK(!pending_scan);
    reset_test(true);
    command("GUI", "TOPO_RUN 123 peer 11 1 0"); CHECK(strncmp(last_reply, "ERR ", 4) == 0);
    command("GUI", "TOPO_POINT 123 peer 1 24 0 0"); CHECK(strncmp(last_reply, "ERR ", 4) == 0);
    command("GUI", "TOPO_POINT 123 peer 1 0 240 0"); CHECK(strncmp(last_reply, "ERR ", 4) == 0);
    CHECK(!pending_scan && !measurements && lock_errors == 0);
    return 0;
}

EXPORT int topology_test_disconnect_rejects_old_session(void)
{
    reset_test(false);
    command("GUI", "TOPO_BEGIN 123 1 1"); CHECK(!strcmp(last_reply, "OK TOPO_BEGIN"));
    topology_scan_disconnected();
    command("GUI", "TOPO_MASK 123 0 0 000001"); CHECK(strstr(last_reply, "RECOVERY_PENDING"));
    command("GUI", "TOPO_BEGIN 124 1 1"); CHECK(strstr(last_reply, "RECOVERY_PENDING"));
    CHECK(s_plan.session == 123 && s_plan.epoch != epoch());
    CHECK(topology_scan_debug_lock() != ESP_OK);
    CHECK(lock_errors == 0);
    return 0;
}

EXPORT int topology_test_source_lease_between_points(void)
{
    reset_test(true);
    command("GUI", "TOPO_RUN 123 peer 1 1 0"); CHECK(pending_scan);
    pending_scan(NULL);
    CHECK(!strcmp(last_reply, "TOPO_DONE 123 24"));
    CHECK(topology_scan_debug_lock() != ESP_OK);
    command("stranger", "TOPO_POINT 123 peer 1 0 0 0"); CHECK(strstr(last_reply, "SESSION_MISMATCH"));
    command("GUI", "TOPO_POINT 124 peer 1 0 0 0"); CHECK(strstr(last_reply, "SESSION_MISMATCH"));
    command("GUI", "TOPO_POINT 123 peer 1 0 0 0"); CHECK(!strcmp(last_reply, "OK TOPO_POINT"));
    pending_scan(NULL);
    CHECK(!strcmp(last_reply, "TOPO_DONE 123 1"));
    command("GUI", "TOPO_RESET 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESET"));
    CHECK(topology_scan_debug_lock() == ESP_OK);
    topology_scan_debug_unlock();
    CHECK(lock_errors == 0);
    return 0;
}

static unsigned pause_visits, pause_errors, expected_measurements, expected_commits;

static void abort_stuck_pause(void)
{
    ++pause_errors;
    s_cancel = true;
    delay_hook = NULL;
}

static void resume_disconnected_job(void)
{
    ++pause_visits;
    delay_hook = abort_stuck_pause;
    if (active_source != -1 || measurements != expected_measurements ||
        journal_info.next - 1 != expected_commits) ++pause_errors;
    command("stranger", "TOPO_RESUME 123");
    if (strncmp(last_reply, "ERR ", 4)) ++pause_errors;
    command("GUI", "TOPO_FETCH 123 1 4");
    if (strncmp(last_reply, "OK TOPO_FETCH ", 14)) ++pause_errors;
    command("GUI", "TOPO_RESUME 123");
    if (strcmp(last_reply, "OK TOPO_RESUME")) ++pause_errors;
}

/* Every coordinate has exactly one durable record, even when acquisition retries. */
static int check_reliable_records(unsigned rounds)
{
    unsigned count = rounds * 24;
    CHECK(journal_info.next == count + 2 && journal_info.used_records == count + 1 - journal_info.ack);
    for (unsigned index = 0; index < count; ++index) {
        unsigned session, source, round;
        const topology_journal_record_t *record = &journal_records[index];
        CHECK(record->sequence == index + 1 && record->session == 123 && record->job_id == 1);
        CHECK(sscanf(record->payload, "TOPO_SAMPLE %u %u %u", &session, &source, &round) == 3);
        CHECK(session == 123 && source == index % 24 && round == index / 24);
    }
    char terminal[64];
    snprintf(terminal, sizeof(terminal), "TOPO_DONE 123 %u", count);
    CHECK(!strcmp(journal_records[count].payload, terminal));
    CHECK(active_source == -1 && lock_errors == 0);
    return 0;
}

EXPORT int topology_test_reliable_commit_without_result_delivery(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123"); CHECK(!strncmp(last_reply, "OK TOPO_OPEN", 12));
    command("GUI", "TOPO_RUN2 123 1 peer 1 2 0"); CHECK(pending_scan);
    result_transport_fails = true;
    pending_scan(NULL);
    CHECK(measurements == 48);
    return check_reliable_records(2);
}

EXPORT int topology_test_reliable_disconnect_before_commit_retries_once(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RUN2 123 1 peer 1 2 0"); CHECK(pending_scan);
    disconnect_during_measurement = 3;
    pause_visits = pause_errors = 0;
    expected_measurements = 3; expected_commits = 2;
    delay_hook = resume_disconnected_job;
    pending_scan(NULL);
    CHECK(pause_visits == 1 && pause_errors == 0);
    CHECK(measurements == 49);
    return check_reliable_records(2);
}

EXPORT int topology_test_reliable_disconnect_after_commit_keeps_next_coordinate(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RUN2 123 1 peer 1 2 0"); CHECK(pending_scan);
    disconnect_after_commit = 3;
    pause_visits = pause_errors = 0;
    expected_measurements = expected_commits = 3;
    delay_hook = resume_disconnected_job;
    pending_scan(NULL);
    CHECK(pause_visits == 1 && pause_errors == 0);
    CHECK(measurements == 48);
    return check_reliable_records(2);
}

EXPORT int topology_test_reliable_fetch_ack_reset_retention(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RUN2 123 1 peer 1 1 0"); CHECK(pending_scan);
    pending_scan(NULL);
    CHECK(check_reliable_records(1) == 0);
    reply_count = 0;
    command("GUI", "TOPO_FETCH 123 1 4");
    CHECK(reply_count == 5 && !strncmp(replies[0], "TOPO_DATA 123 1 1 ", 18));
    char first_fetch[5][TEXT_SIZE];
    memcpy(first_fetch, replies, sizeof(first_fetch));
    reply_count = 0;
    command("GUI", "TOPO_FETCH 123 1 4");
    CHECK(reply_count == 5 && !memcmp(first_fetch, replies, sizeof(first_fetch)));
    CHECK(journal_info.ack == 0 && journal_info.used_records == 25);
    command("stranger", "TOPO_FETCH 123 1 4"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", "TOPO_ACK 123 4 00000000"); CHECK(!strncmp(last_reply, "ERR ", 4));
    CHECK(journal_info.ack == 0 && journal_info.used_records == 25);
    char ack[80];
    snprintf(ack, sizeof(ack), "TOPO_ACK 123 4 %08x", (unsigned)journal_records[3].crc);
    command("stranger", ack); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", ack); CHECK(!strncmp(last_reply, "OK TOPO_ACK", 11));
    CHECK(journal_info.ack == 4 && journal_info.used_records == 21);
    command("GUI", "TOPO_RESET 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESET"));
    CHECK(journal_info.ack == 4 && journal_info.used_records == 21);
    command("GUI", "TOPO_OPEN 456"); CHECK(!strncmp(last_reply, "ERR ", 4));
    CHECK(journal_info.session == 123 && journal_info.used_records == 21);
    snprintf(ack, sizeof(ack), "TOPO_ACK 123 25 %08x", (unsigned)journal_records[24].crc);
    command("GUI", ack); CHECK(!strncmp(last_reply, "OK TOPO_ACK", 11));
    command("GUI", "TOPO_OPEN 456"); CHECK(!strncmp(last_reply, "OK TOPO_OPEN", 12));
    CHECK(journal_info.session == 456 && journal_info.used_records == 0);
    return 0;
}

EXPORT int topology_test_reliable_receiver_plan_survives_disconnect(void)
{
    reset_test(false);
    command("GUI", "TOPO_BEGIN2 123 1 2"); CHECK(!strcmp(last_reply, "OK TOPO_BEGIN2"));
    command("GUI", "TOPO_MASK 123 0 0 000001");
    command("GUI", "TOPO_MASK 123 1 0 000004");
    command("GUI", "TOPO_SEAL 123"); CHECK(!strcmp(last_reply, "OK TOPO_SEAL"));
    topology_scan_disconnected();
    command("peer", "TOPO_PREP 123 1"); CHECK(strstr(last_reply, "PLAN_PAUSED"));
    command("stranger", "TOPO_RESUME 123"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", "TOPO_RESUME 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESUME"));
    command("peer", "TOPO_PREP 123 1"); CHECK(!strcmp(last_reply, "OK TOPO_PREP"));
    CHECK(remote_masks[0] == 4 && s_plan.masks[0][0] == 1 && lock_errors == 0);
    return 0;
}

static void drain_paused_journal(void)
{
    ++pause_visits;
    if (active_source != -1 || measurements != 3 || journal_info.next != 4) ++pause_errors;
    if (pause_visits == 1) {
        command("GUI", "TOPO_RESUME 123");
        if (strcmp(last_reply, "OK TOPO_RESUME")) ++pause_errors;
    } else if (pause_visits == 2) {
        char ack[80];
        snprintf(ack, sizeof(ack), "TOPO_ACK 123 3 %08x", (unsigned)journal_records[2].crc);
        command("GUI", ack);
        if (strncmp(last_reply, "OK TOPO_ACK", 11) || journal_info.used_records) ++pause_errors;
        journal_paused = false;
        delay_hook = abort_stuck_pause;
    } else {
        abort_stuck_pause();
    }
}

EXPORT int topology_test_reliable_cache_pause_requires_drain_before_next_sample(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RUN2 123 1 peer 1 2 0"); CHECK(pending_scan);
    pause_after_commit = 3;
    pause_visits = pause_errors = 0;
    delay_hook = drain_paused_journal;
    pending_scan(NULL);
    CHECK(pause_visits == 2 && pause_errors == 0 && measurements == 48);
    CHECK(journal_info.ack == 3);
    return check_reliable_records(2);
}

EXPORT int topology_test_reliable_repeated_job_is_idempotent(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RUN2 123 1 peer 1 1 0"); CHECK(pending_scan);
    command("GUI", "TOPO_RUN2 123 1 peer 1 1 0"); CHECK(!strcmp(last_reply, "OK TOPO_RUN2"));
    CHECK(scan_tasks_created == 1);
    pending_scan(NULL);
    CHECK(check_reliable_records(1) == 0);
    command("GUI", "TOPO_RUN2 123 1 peer 1 1 0"); CHECK(!strcmp(last_reply, "OK TOPO_RUN2"));
    CHECK(scan_tasks_created == 1 && measurements == 24 && journal_info.next == 26);
    command("GUI", "TOPO_RUN2 123 1 peer 1 2 0"); CHECK(strstr(last_reply, "JOB_MISMATCH"));
    command("GUI", "TOPO_POINT2 123 3 peer 1 0 0 0"); CHECK(strstr(last_reply, "JOB_SEQUENCE"));
    command("GUI", "TOPO_POINT2 123 2 peer 1 0 0 0"); CHECK(!strcmp(last_reply, "OK TOPO_POINT2"));
    CHECK(scan_tasks_created == 2);
    pending_scan(NULL);
    CHECK(measurements == 25 && journal_info.next == 28);
    CHECK(journal_records[25].job_id == 2 && !strncmp(journal_records[25].payload, "TOPO_POINT_SAMPLE 123 0 0 ", 25));
    CHECK(!strcmp(journal_records[26].payload, "TOPO_DONE 123 1"));
    CHECK(lock_errors == 0);
    return 0;
}

EXPORT int topology_test_reliable_boot_recovers_upload_without_resuming_hardware(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RUN2 123 1 peer 1 1 0"); CHECK(pending_scan);
    pending_scan(NULL);
    CHECK(check_reliable_records(1) == 0);
    /* Recreate volatile controller state while the committed storage image stays. */
    memset(&s_plan, 0, sizeof(s_plan)); memset(&s_job, 0, sizeof(s_job));
    s_lock = NULL; s_commands = NULL; s_peer_replies = NULL;
    s_journal_ready = false; s_cache_session = 0; s_cache_owner[0] = '\0';
    s_cache_recovered = false; s_epoch = 0; s_cancel = false;
    queue_count = 0; pending_scan = NULL; journal_info.recovered = true;
    CHECK(topology_scan_init() == ESP_OK);
    command("GUI", "TOPO_OPEN 123"); CHECK(!strcmp(last_reply, "OK TOPO_OPEN"));
    command("GUI", "TOPO_FETCH 123 1 4"); CHECK(strstr(last_reply, "state=RECOVERED"));
    command("GUI", "TOPO_RESUME 123"); CHECK(strstr(last_reply, "REBOOT_REQUIRES_NEW_SCAN"));
    command("GUI", "TOPO_RUN2 123 1 peer 1 1 0"); CHECK(strstr(last_reply, "REBOOT_REQUIRES_NEW_SCAN"));
    CHECK(!pending_scan && measurements == 24 && journal_info.used_records == 25);
    CHECK(active_source == -1 && lock_errors == 0);
    return 0;
}

/* Exercise host recovery ordering against the real C controller after reboot.
 * Device responses are simulated; this checks scope/ownership and journal retention,
 * not UART timing. Both fully acknowledged and pending caches must remain intact.
 */
EXPORT int topology_test_recovered_cache_requires_original_module_discovery(void)
{
    for (unsigned acknowledged = 0; acknowledged < 2; ++acknowledged) {
        reset_test(true);
        online_modules = 1;
        command("GUI", "TOPO_DISCOVER 1");
        CHECK(!strcmp(last_reply, "OK TOPO_DISCOVER count=1 online=00000001"));
        command("GUI", "TOPO_OPEN 123");
        command("GUI", "TOPO_RUN2 123 1 peer 1 1 0"); CHECK(pending_scan);
        pending_scan(NULL);
        CHECK(check_reliable_records(1) == 0);
        if (acknowledged) {
            char ack[80];
            snprintf(ack, sizeof(ack), "TOPO_ACK 123 25 %08x", (unsigned)journal_records[24].crc);
            command("GUI", ack); CHECK(!strcmp(last_reply, "OK TOPO_ACK"));
        }
        topology_journal_record_t saved_record = journal_records[0];
        unsigned saved_used = journal_info.used_records;
        uint32_t saved_ack = journal_info.ack, saved_next = journal_info.next;

        /* Restore only durable cache metadata, as actual boot does. The prior
         * module selection was RAM and therefore returns to the default seven.
         */
        memset(&s_plan, 0, sizeof(s_plan)); memset(&s_job, 0, sizeof(s_job));
        s_lock = NULL; s_commands = NULL; s_peer_replies = NULL;
        s_selected_modules = TOPOLOGY_DEFAULT_MODULES;
        s_journal_ready = false; s_cache_session = 0; s_cache_owner[0] = '\0';
        s_cache_recovered = false; s_epoch = 0; s_cancel = false;
        queue_count = 0; pending_scan = NULL; journal_info.recovered = true;
        CHECK(topology_scan_init() == ESP_OK);
        CHECK(s_cache_recovered && s_cache_session == 123);
        command("GUI", "TOPO_RESET 123");
        CHECK(!strcmp(last_reply, "ERR TOPO_RESET ESP_ERR_TIMEOUT"));
        CHECK(last_reset_count == TOPOLOGY_DEFAULT_MODULES && s_cache_session == 123);
        CHECK(topology_scan_debug_lock() != ESP_OK);
        CHECK(journal_info.used_records == saved_used && journal_info.ack == saved_ack);

        command("GUI", "TOPO_DISCOVER 1");
        CHECK(!strcmp(last_reply, "OK TOPO_DISCOVER count=1 online=00000001"));
        CHECK(s_cache_session == 123 && topology_scan_debug_lock() != ESP_OK);
        command("GUI", "TOPO_RESET 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESET"));
        CHECK(last_reset_count == 1 && s_cache_session == 0);
        CHECK(topology_scan_debug_lock() == ESP_OK);
        topology_scan_debug_unlock();
        CHECK(journal_info.session == 123 && journal_info.next == saved_next);
        CHECK(journal_info.used_records == saved_used && journal_info.ack == saved_ack);
        CHECK(!memcmp(&saved_record, &journal_records[0], sizeof(saved_record)));
        if (!acknowledged) {
            command("GUI", "TOPO_OPEN 456"); CHECK(strncmp(last_reply, "ERR ", 4) == 0);
            CHECK(journal_info.session == 123 && journal_info.used_records == saved_used);
        }
        CHECK(active_source == -1 && lock_errors == 0);
    }
    return 0;
}

/* A live receiver retains its original cleanup scope. A proposed smaller scan
 * may not replace that scope before the previous plan has been reset.
 */
EXPORT int topology_test_discovery_cannot_shrink_existing_receiver_plan(void)
{
    reset_test(false);
    online_modules = 2;
    command("GUI", "TOPO_DISCOVER 2");
    CHECK(!strcmp(last_reply, "OK TOPO_DISCOVER count=2 online=00000003"));
    command("GUI", "TOPO_BEGIN2 123 2 1"); CHECK(!strcmp(last_reply, "OK TOPO_BEGIN2"));
    command("GUI", "TOPO_MASK 123 0 0 000001");
    command("GUI", "TOPO_MASK 123 0 1 000002");
    command("GUI", "TOPO_SEAL 123"); CHECK(!strcmp(last_reply, "OK TOPO_SEAL"));
    command("peer", "TOPO_PREP2 123 0 1"); CHECK(!strcmp(last_reply, "OK TOPO_PREP2 1"));
    CHECK(remote_masks[0] == 1 && remote_masks[1] == 2);
    command("GUI", "TOPO_DISCOVER 1"); CHECK(!strcmp(last_reply, "ERR TOPO_DISCOVER BUSY"));
    CHECK(s_selected_modules == 2 && s_plan.modules == 2 && s_plan.session == 123);
    CHECK(remote_masks[0] == 1 && remote_masks[1] == 2);
    command("GUI", "TOPO_RESET 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESET"));
    CHECK(last_reset_count == 2 && remote_masks[0] == 0 && remote_masks[1] == 0);
    CHECK(topology_scan_debug_lock() == ESP_OK);
    topology_scan_debug_unlock();
    CHECK(lock_errors == 0);
    return 0;
}

EXPORT int topology_test_reliable_receiver_change_discards_inflight_sample(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RUN2 123 1 peer 1 2 0"); CHECK(pending_scan);
    invalidate_peer_during_measurement = 3;
    pause_visits = pause_errors = 0;
    expected_measurements = 3; expected_commits = 2;
    delay_hook = resume_disconnected_job;
    pending_scan(NULL);
    CHECK(pause_visits == 1 && pause_errors == 0 && measurements == 49);
    CHECK(epoch() == 0);
    return check_reliable_records(2);
}

EXPORT int topology_test_reliable_receiver_selection_validation_lifetime(void)
{
    reset_test(false);
    command("GUI", "TOPO_BEGIN2 123 1 1");
    command("GUI", "TOPO_MASK 123 0 0 000001");
    command("GUI", "TOPO_SEAL 123");
    command("peer", "TOPO_PREP2 123 0 41"); CHECK(!strcmp(last_reply, "OK TOPO_PREP2 41"));
    command("peer", "TOPO_VALIDATE 123 41"); CHECK(!strcmp(last_reply, "OK TOPO_VALIDATE"));
    command("stranger", "TOPO_VALIDATE 123 41"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", "TOPO_RESUME 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESUME"));
    command("peer", "TOPO_VALIDATE 123 41"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("peer", "TOPO_PREP2 123 0 42"); CHECK(!strcmp(last_reply, "OK TOPO_PREP2 42"));
    command("peer", "TOPO_CLEAR 123"); CHECK(!strcmp(last_reply, "OK TOPO_CLEAR"));
    command("peer", "TOPO_VALIDATE 123 42"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("peer", "TOPO_PICK2 123 0 43"); CHECK(!strcmp(last_reply, "OK TOPO_PICK2 43"));
    topology_scan_disconnected();
    command("peer", "TOPO_VALIDATE 123 43"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", "TOPO_RESUME 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESUME"));
    command("peer", "TOPO_PREP2 123 0 44"); CHECK(!strcmp(last_reply, "OK TOPO_PREP2 44"));
    command("peer", "TOPO_PREP2 123 0 45"); CHECK(!strcmp(last_reply, "OK TOPO_PREP2 45"));
    command("peer", "TOPO_VALIDATE 123 44"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("peer", "TOPO_VALIDATE 123 45"); CHECK(!strcmp(last_reply, "OK TOPO_VALIDATE"));
    fake_time += 25LL * 1000000LL;
    command("peer", "TOPO_VALIDATE 123 45"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", "TOPO_RESET 123"); CHECK(!strcmp(last_reply, "OK TOPO_RESET"));
    command("peer", "TOPO_VALIDATE 123 45"); CHECK(!strncmp(last_reply, "ERR ", 4));
    CHECK(lock_errors == 0);
    return 0;
}

EXPORT int topology_test_binary_span_cross_module_and_token(void)
{
    reset_test(false);
    command("GUI", "TOPO_BEGIN2 123 10 1");
    char text[80];
    for (unsigned module = 0; module < 10; ++module) {
        snprintf(text, sizeof(text), "TOPO_MASK 123 0 %u 000000", module);
        command("GUI", text);
    }
    command("GUI", "TOPO_SEAL 123"); CHECK(!strcmp(last_reply, "OK TOPO_SEAL"));
    command("peer", "TOPO_SPAN2 123 23 25 81"); CHECK(!strcmp(last_reply, "OK TOPO_SPAN2 81"));
    CHECK(remote_masks[0] == 0x800000 && remote_masks[1] == 1 && remote_masks[9] == 0);
    command("peer", "TOPO_VALIDATE 123 81"); CHECK(!strcmp(last_reply, "OK TOPO_VALIDATE"));
    command("stranger", "TOPO_SPAN2 123 0 24 82"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("peer", "TOPO_SPAN2 123 239 240 82"); CHECK(!strcmp(last_reply, "OK TOPO_SPAN2 82"));
    CHECK(remote_masks[0] == 0 && remote_masks[1] == 0 && remote_masks[9] == 0x800000);
    command("peer", "TOPO_VALIDATE 123 81"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("peer", "TOPO_SPAN2 123 24 24 83"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("peer", "TOPO_SPAN2 123 0 241 83"); CHECK(!strncmp(last_reply, "ERR ", 4));
    topology_scan_disconnected();
    command("peer", "TOPO_SPAN2 123 0 24 83"); CHECK(!strcmp(last_reply, "ERR TOPO_SPAN2 PLAN_PAUSED"));
    command("GUI", "TOPO_RESUME 123");
    command("peer", "TOPO_VALIDATE 123 82"); CHECK(!strncmp(last_reply, "ERR ", 4));
    CHECK(lock_errors == 0);
    return 0;
}

EXPORT int topology_test_binary_range_identity_and_durable_record(void)
{
    reset_test(true);
    command("GUI", "TOPO_INFO"); CHECK(strstr(last_reply, "binary=1") && strstr(last_reply, "cache_error=ESP_OK"));
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RANGE2 123 1 peer 2 24 23 25 0"); CHECK(!strcmp(last_reply, "OK TOPO_RANGE2"));
    CHECK(pending_scan && scan_tasks_created == 1);
    command("GUI", "TOPO_RANGE2 123 1 peer 2 24 23 25 0"); CHECK(scan_tasks_created == 1);
    command("GUI", "TOPO_RANGE2 123 1 peer 2 24 23 26 0"); CHECK(!strcmp(last_reply, "ERR TOPO_RANGE2 JOB_MISMATCH"));
    command("stranger", "TOPO_RANGE2 123 1 peer 2 24 23 25 0"); CHECK(!strncmp(last_reply, "ERR ", 4));
    pending_scan(NULL);
    CHECK(measurements == 1 && journal_info.next == 3 && active_source == -1);
    CHECK(strstr(journal_records[0].payload, "TOPO_RANGE_SAMPLE 123 24 23 25 OK MEASURE"));
    CHECK(!strcmp(journal_records[1].payload, "TOPO_DONE 123 1"));
    command("GUI", "TOPO_RANGE2 123 1 peer 2 24 23 25 0"); CHECK(scan_tasks_created == 1);
    command("GUI", "TOPO_RANGE2 123 2 peer 2 48 23 25 0"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", "TOPO_RANGE2 123 2 peer 2 24 25 25 0"); CHECK(!strncmp(last_reply, "ERR ", 4));
    command("GUI", "TOPO_POINT2 123 2 peer 2 24 239 0"); CHECK(!strcmp(last_reply, "OK TOPO_POINT2"));
    pending_scan(NULL);
    CHECK(measurements == 2 && journal_info.next == 5 && lock_errors == 0);
    return 0;
}

EXPORT int topology_test_binary_range_disconnect_before_commit(void)
{
    reset_test(true);
    command("GUI", "TOPO_OPEN 123");
    command("GUI", "TOPO_RANGE2 123 1 peer 1 0 0 24 0"); CHECK(pending_scan);
    disconnect_during_measurement = 1;
    pause_visits = pause_errors = 0;
    expected_measurements = 1; expected_commits = 0;
    delay_hook = resume_disconnected_job;
    pending_scan(NULL);
    CHECK(pause_visits == 1 && pause_errors == 0 && measurements == 2);
    CHECK(journal_info.next == 3 && strstr(journal_records[0].payload, "TOPO_RANGE_SAMPLE 123 0 0 24 "));
    CHECK(lock_errors == 0 && active_source == -1);
    return 0;
}
