#include "topology_scan.h"

#include <errno.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ch446.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "rs485_master.h"
#include "topology_config.h"
#include "topology_journal.h"
#include "wifi_server.h"
#include "xd31h.h"

#define TEXT_SIZE 320U
#define ID_SIZE 32U
#define PEER_TIMEOUT_US (15LL * 1000000LL)
#define PLAN_TIMEOUT_US (60LL * 1000000LL)
/* Receiver masks expire at 30 seconds; validate against the earliest module write. */
#define SELECTION_VALID_US (25LL * 1000000LL)

typedef struct {
    char text[224];
    char source[ID_SIZE];
    char request[ID_SIZE];
    uint32_t epoch;
} command_t;

typedef struct {
    uint32_t session;
    uint32_t epoch;
    unsigned modules;
    unsigned rounds;
    uint32_t masks[TOPOLOGY_MAX_ROUNDS][TOPOLOGY_MAX_MODULES];
    uint16_t loaded[TOPOLOGY_MAX_ROUNDS];
    bool sealed;
    bool reliable;
    bool needs_resume;
    uint32_t cleared_epoch;
    uint32_t selection_token;
    int64_t prepared_at;
    char owner[ID_SIZE];
    char peer[ID_SIZE];
    int64_t touched;
} plan_t;

typedef struct {
    bool active;
    bool point;
    bool range;
    bool reliable;
    bool paused;
    uint32_t job_id;
    uint32_t resume_epoch;
    uint32_t resume_generation;
    const char *pause_reason;
    const char *state;
    uint32_t epoch;
    uint32_t session;
    unsigned modules;
    unsigned rounds;
    unsigned source_port;
    unsigned destination_port;
    unsigned range_end;
    unsigned settle_ms;
    int64_t touched;
    char owner[ID_SIZE];
    char request[ID_SIZE];
    char peer[ID_SIZE];
} job_t;

static SemaphoreHandle_t s_lock;
static QueueHandle_t s_commands;
static QueueHandle_t s_peer_replies;
static plan_t s_plan;
static job_t s_job;
static portMUX_TYPE s_flags_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t s_epoch;
static bool s_cancel;
static uint32_t s_sequence;
static uint32_t s_bus_step;
static uint32_t s_selection_sequence;
static unsigned s_selected_modules = TOPOLOGY_DEFAULT_MODULES;
static char s_wait_peer[ID_SIZE];
static char s_wait_request[ID_SIZE];
static bool s_journal_ready;
static esp_err_t s_journal_error;
static uint32_t s_cache_session;
static char s_cache_owner[ID_SIZE];
static bool s_cache_recovered;

static void take(void) { xSemaphoreTake(s_lock, portMAX_DELAY); }
static void give(void) { xSemaphoreGive(s_lock); }

static uint32_t epoch(void)
{
    portENTER_CRITICAL(&s_flags_lock);
    uint32_t result = s_epoch;
    portEXIT_CRITICAL(&s_flags_lock);
    return result;
}

static bool cancelled(const job_t *job)
{
    portENTER_CRITICAL(&s_flags_lock);
    bool result = s_cancel || (!job->reliable && job->epoch != s_epoch);
    portEXIT_CRITICAL(&s_flags_lock);
    return result;
}

static esp_err_t send_reply(const char *owner, const char *request, const char *format, ...)
{
    char text[TEXT_SIZE];
    va_list args;
    va_start(args, format);
    int length = vsnprintf(text, sizeof(text), format, args);
    va_end(args);
    if (length < 0 || (size_t)length >= sizeof(text)) return ESP_ERR_INVALID_SIZE;
    return wifi_server_send_result(owner, request, text);
}

static bool parse_number(const char *text, unsigned base, uint32_t maximum, uint32_t *value)
{
    if (!text || !*text || *text == '-' || *text == '+') return false;
    errno = 0;
    char *end;
    unsigned long parsed = strtoul(text, &end, base);
    if (errno || *end || parsed > maximum) return false;
    *value = (uint32_t)parsed;
    return true;
}

static esp_err_t clear_local(unsigned modules)
{
    /* Downstream contacts open the circuit; master1 retains its fixed input path. */
    esp_err_t local = ch446_reset_all();
    esp_err_t slaves = rs485_master_reset_modules(modules);
    return local != ESP_OK ? local : slaves;
}

static esp_err_t apply_module(unsigned module, uint32_t session, uint32_t mask, bool positive)
{
    if (s_bus_step == UINT32_MAX) return ESP_ERR_INVALID_STATE;
    return rs485_master_apply_mask(module, session, ++s_bus_step, mask, positive);
}

static esp_err_t reserve_source(const job_t *job)
{
    for (unsigned module = 0; module < job->modules; ++module) {
        esp_err_t error = apply_module(module, job->session, 0, true);
        if (error != ESP_OK) return error;
    }
    return ESP_OK;
}

/* One scan worker owns this mailbox; the receive task only delivers correlated frames. */
static esp_err_t peer_request(const job_t *job, bool cleanup, char *response,
                              const char *format, ...)
{
    char text[224];
    response[0] = '\0';
    if (job->epoch != epoch()) return ESP_ERR_INVALID_STATE;
    char request[ID_SIZE];
    va_list args;
    va_start(args, format);
    int length = vsnprintf(text, sizeof(text), format, args);
    va_end(args);
    if (length < 0 || (size_t)length >= sizeof(text)) return ESP_ERR_INVALID_SIZE;
    take();
    snprintf(request, sizeof(request), "TP-%08" PRIx32 "-%08" PRIx32, job->session, ++s_sequence);
    xQueueReset(s_peer_replies);
    snprintf(s_wait_peer, sizeof(s_wait_peer), "%s", job->peer);
    snprintf(s_wait_request, sizeof(s_wait_request), "%s", request);
    give();
    esp_err_t error = wifi_server_send_request(job->peer, request, text);
    if (error == ESP_OK) {
        int64_t deadline = esp_timer_get_time() + PEER_TIMEOUT_US;
        error = ESP_ERR_TIMEOUT;
        while (esp_timer_get_time() < deadline) {
            if (xQueueReceive(s_peer_replies, response, pdMS_TO_TICKS(50)) == pdTRUE) {
                error = strncmp(response, "OK ", 3) == 0 ? ESP_OK : ESP_FAIL;
                break;
            }
            if (job->epoch != epoch() || (!cleanup && cancelled(job))) {
                error = ESP_ERR_INVALID_STATE;
                break;
            }
        }
    }
    take(); s_wait_peer[0] = '\0'; s_wait_request[0] = '\0'; give();
    return error;
}

static esp_err_t settle(const job_t *job)
{
    int64_t deadline = esp_timer_get_time() + job->settle_ms * 1000LL;
    while (esp_timer_get_time() < deadline) {
        if (cancelled(job) || job->epoch != epoch()) return ESP_ERR_INVALID_STATE;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return cancelled(job) || job->epoch != epoch() ? ESP_ERR_INVALID_STATE : ESP_OK;
}

static void measurement_text(char *raw, size_t size)
{
    xd31h_measurement_t value;
    esp_err_t error = xd31h_read_measurement(&value, XD31H_DEFAULT_TIMEOUT_MS);
    if (error == ESP_OK && value.valid) {
        snprintf(raw, size, "OK MEASURE resistance=%.3f raw=%u range=%u",
                 value.resistance_ohm, value.raw_value, value.range);
    } else {
        char diagnostic[XD31H_DIAGNOSTIC_TEXT_SIZE];
        xd31h_format_diagnostic(&value, error, diagnostic, sizeof(diagnostic));
        snprintf(raw, size, "ERR MEASURE %s", diagnostic);
    }
}

static esp_err_t measure(const job_t *job, unsigned source, unsigned index)
{
    char raw[160];
    measurement_text(raw, sizeof(raw));
    return send_reply(job->owner, job->request, "%s %" PRIu32 " %u %u %s",
                      job->point ? "TOPO_POINT_SAMPLE" : "TOPO_SAMPLE",
                      job->session, source, index, raw);
}

static void scan_task(void *argument)
{
    (void)argument;
    take(); job_t job = s_job; give();
    unsigned completed = 0;
    char response[TEXT_SIZE] = "";
    esp_err_t error = clear_local(job.modules);
    if (error == ESP_OK) error = reserve_source(&job);
    int64_t last_reservation = esp_timer_get_time();
    unsigned first = job.point ? job.source_port : 0;
    unsigned end = job.point ? first + 1 : job.modules * TOPOLOGY_GROUPS_PER_MODULE;
    for (unsigned round = 0; round < job.rounds && error == ESP_OK; ++round) {
        int64_t last_remote_prepare = 0;
        for (unsigned source = first; source < end && error == ESP_OK; ++source) {
            if (cancelled(&job)) { error = ESP_ERR_INVALID_STATE; break; }
            if (esp_timer_get_time() - last_reservation > 5LL * 1000000LL) {
                error = reserve_source(&job);
                last_reservation = esp_timer_get_time();
                if (error != ESP_OK) break;
            }
            if (!last_remote_prepare || esp_timer_get_time() - last_remote_prepare > 5LL * 1000000LL) {
                error = peer_request(&job, false, response,
                    job.point ? "TOPO_PICK %" PRIu32 " %u" : "TOPO_PREP %" PRIu32 " %u",
                    job.session, job.point ? job.destination_port : round);
                const char *ready = job.point ? "OK TOPO_PICK" : "OK TOPO_PREP";
                if (error == ESP_OK && strcmp(response, ready)) error = ESP_ERR_INVALID_RESPONSE;
                last_remote_prepare = esp_timer_get_time();
            }
            if (error != ESP_OK || cancelled(&job)) break;
            unsigned module = source / TOPOLOGY_GROUPS_PER_MODULE;
            error = apply_module(module, job.session, 1UL << (source % TOPOLOGY_GROUPS_PER_MODULE), true);
            if (error == ESP_OK) error = settle(&job);
            if (error == ESP_OK) {
                error = measure(&job, source, job.point ? job.destination_port : round);
                if (error == ESP_OK) ++completed;
            }
            esp_err_t opened = apply_module(module, job.session, 0, true);
            if (error == ESP_OK) error = opened;
        }
    }
    bool stopped = cancelled(&job);
    esp_err_t cleanup = clear_local(job.modules);
    esp_err_t remote = peer_request(&job, true, response, "TOPO_CLEAR %" PRIu32, job.session);
    if (remote == ESP_OK && strcmp(response, "OK TOPO_CLEAR")) remote = ESP_ERR_INVALID_RESPONSE;
    if (cleanup != ESP_OK || remote != ESP_OK) {
        stopped = false;
        error = cleanup != ESP_OK ? cleanup : remote;
    }
    take();
    s_job.active = false;
    s_job.touched = esp_timer_get_time();
    // Queue the terminal result before another job may take over these endpoints.
    if (job.epoch != epoch()) {
        give(); vTaskDelete(NULL); return;
    }
    if (stopped) send_reply(job.owner, job.request, "TOPO_STOPPED %" PRIu32 " %u", job.session, completed);
    else if (error != ESP_OK) send_reply(job.owner, job.request,
        "TOPO_FAILED %" PRIu32 " error=%s local_reset=%s remote_reset=%s",
        job.session, esp_err_to_name(error), esp_err_to_name(cleanup), esp_err_to_name(remote));
    else send_reply(job.owner, job.request, "TOPO_DONE %" PRIu32 " %u", job.session, completed);
    give();
    vTaskDelete(NULL);
}

/* A paused acquisition releases contacts while its immutable journal remains readable. */
static esp_err_t reliable_wait(job_t *job, const char *reason, bool require_resume)
{
    bool cache_paused = topology_journal_should_pause();
    if (!reason && !cache_paused && job->epoch == epoch()) return ESP_OK;
    take();
    uint32_t ticket = s_job.resume_generation;
    s_job.paused = true;
    s_job.state = "PAUSED";
    s_job.pause_reason = reason ? reason : (job->epoch != epoch() ? "NETWORK" : "CACHE");
    give();
    esp_err_t error = clear_local(job->modules);
    if (error != ESP_OK) return error;
    while (!cancelled(job)) {
        uint32_t current_epoch = epoch();
        take();
        bool network_ready = job->epoch == current_epoch || s_job.resume_epoch == current_epoch;
        bool control_ready = !require_resume || s_job.resume_generation != ticket;
        give();
        if (network_ready && control_ready && !topology_journal_should_pause()) {
            job->epoch = current_epoch;
            error = reserve_source(job);
            if (error != ESP_OK) return error;
            take();
            s_job.epoch = current_epoch;
            s_job.paused = false;
            s_job.state = "RUNNING";
            s_job.pause_reason = "NONE";
            give();
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    return ESP_ERR_INVALID_STATE;
}

static bool peer_interrupted(const job_t *job, esp_err_t error, const char *response)
{
    return job->epoch != epoch() || error == ESP_ERR_TIMEOUT ||
           (error != ESP_OK && (!response[0] || strstr(response, "PLAN_PAUSED")));
}

/* Network delivery never determines whether a successfully committed sample exists. */
static void reliable_scan_task(void *argument)
{
    (void)argument;
    take(); job_t job = s_job; give();
    unsigned completed = 0;
    uint32_t sequence, checksum;
    char response[TEXT_SIZE] = "";
    esp_err_t error = clear_local(job.modules);
    if (error == ESP_OK) error = reserve_source(&job);
    int64_t last_reservation = esp_timer_get_time();
    unsigned first = job.point ? job.source_port : 0;
    unsigned end = job.point ? first + 1 : job.modules * TOPOLOGY_GROUPS_PER_MODULE;
    for (unsigned round = 0; round < job.rounds && error == ESP_OK; ++round) {
        unsigned source = first;
        while (source < end && error == ESP_OK) {
            if (cancelled(&job)) break;
            error = reliable_wait(&job, NULL, false);
            if (error != ESP_OK) break;
            if (esp_timer_get_time() - last_reservation > 5LL * 1000000LL) {
                error = reserve_source(&job);
                last_reservation = esp_timer_get_time();
                if (error != ESP_OK) break;
            }
            if (s_selection_sequence == UINT32_MAX) { error = ESP_ERR_INVALID_STATE; break; }
            uint32_t selection = ++s_selection_sequence;
            if (job.range) {
                error = peer_request(&job, false, response, "TOPO_SPAN2 %" PRIu32 " %u %u %" PRIu32,
                    job.session, job.destination_port, job.range_end, selection);
            } else {
                error = peer_request(&job, false, response,
                    job.point ? "TOPO_PICK2 %" PRIu32 " %u %" PRIu32 : "TOPO_PREP2 %" PRIu32 " %u %" PRIu32,
                    job.session, job.point ? job.destination_port : round, selection);
            }
            if (error != ESP_OK && peer_interrupted(&job, error, response) && !cancelled(&job)) {
                bool changed = job.epoch != epoch();
                error = reliable_wait(&job, changed ? "NETWORK" : "CONTROL", !changed);
                continue;
            }
            char ready[64];
            snprintf(ready, sizeof(ready), "OK %s %" PRIu32,
                job.range ? "TOPO_SPAN2" : job.point ? "TOPO_PICK2" : "TOPO_PREP2", selection);
            if (error == ESP_OK && strcmp(response, ready)) error = ESP_ERR_INVALID_RESPONSE;
            if (error != ESP_OK) break;
            unsigned module = source / TOPOLOGY_GROUPS_PER_MODULE;
            error = apply_module(module, job.session, 1UL << (source % TOPOLOGY_GROUPS_PER_MODULE), true);
            if (error == ESP_OK) error = settle(&job);
            char raw[160] = "";
            if (error == ESP_OK) measurement_text(raw, sizeof(raw));
            esp_err_t opened = apply_module(module, job.session, 0, true);
            if (opened != ESP_OK) { error = opened; break; }
            if (cancelled(&job)) { error = ESP_ERR_INVALID_STATE; break; }
            if (job.epoch != epoch()) {
                error = reliable_wait(&job, "NETWORK", false);
                continue;
            }
            if (error != ESP_OK) break;
            error = peer_request(&job, false, response, "TOPO_VALIDATE %" PRIu32 " %" PRIu32, job.session, selection);
            if (error != ESP_OK || strcmp(response, "OK TOPO_VALIDATE")) {
                if (cancelled(&job)) break;
                bool changed = job.epoch != epoch();
                error = reliable_wait(&job, changed ? "NETWORK" : "CONTROL", !changed);
                continue;
            }
            char payload[TOPOLOGY_JOURNAL_PAYLOAD_MAX + 1];
            int size;
            if (job.range) {
                size = snprintf(payload, sizeof(payload), "TOPO_RANGE_SAMPLE %" PRIu32 " %u %u %u %s",
                    job.session, source, job.destination_port, job.range_end, raw);
            } else {
                size = snprintf(payload, sizeof(payload), "%s %" PRIu32 " %u %u %s",
                    job.point ? "TOPO_POINT_SAMPLE" : "TOPO_SAMPLE", job.session, source,
                    job.point ? job.destination_port : round, raw);
            }
            if (size < 0 || (size_t)size >= sizeof(payload)) { error = ESP_ERR_INVALID_SIZE; break; }
            error = topology_journal_append(job.job_id, payload, &sequence, &checksum);
            if (error == ESP_OK) { ++completed; ++source; }
        }
        if (cancelled(&job)) break;
    }
    bool stopped = cancelled(&job);
    esp_err_t local = clear_local(job.modules);
    esp_err_t remote = peer_request(&job, true, response, "TOPO_CLEAR %" PRIu32, job.session);
    if (remote == ESP_OK && strcmp(response, "OK TOPO_CLEAR")) remote = ESP_ERR_INVALID_RESPONSE;
    /* Keep the final record locally even when its remote cleanup needs host recovery. */
    char terminal[TOPOLOGY_JOURNAL_PAYLOAD_MAX + 1];
    const char *state = stopped ? "STOPPED" : error == ESP_OK ? "DONE" : "FAILED";
    if (local != ESP_OK || (remote != ESP_OK && !peer_interrupted(&job, remote, response))) {
        error = local != ESP_OK ? local : remote;
        state = "FAILED";
    }
    if (!strcmp(state, "STOPPED")) {
        snprintf(terminal, sizeof(terminal), "TOPO_STOPPED %" PRIu32 " %u", job.session, completed);
    } else if (error != ESP_OK) {
        snprintf(terminal, sizeof(terminal), "TOPO_FAILED %" PRIu32 " error=%s local_reset=%s remote_reset=%s",
            job.session, esp_err_to_name(error), esp_err_to_name(local), esp_err_to_name(remote));
    } else {
        snprintf(terminal, sizeof(terminal), "TOPO_DONE %" PRIu32 " %u", job.session, completed);
    }
    take();
    esp_err_t stored = topology_journal_append(job.job_id, terminal, &sequence, &checksum);
    s_job.active = false;
    s_job.paused = false;
    s_job.state = stored == ESP_OK ? state : "FAILED";
    s_job.pause_reason = stored == ESP_OK ? "NONE" : "STORAGE";
    s_job.touched = esp_timer_get_time();
    give();
    vTaskDelete(NULL);
}

static esp_err_t prepare_remote(unsigned index, bool point)
{
    s_plan.selection_token = 0;
    s_plan.prepared_at = esp_timer_get_time();
    esp_err_t error = ch446_reset_all();
    for (unsigned module = 0; module < s_plan.modules && error == ESP_OK; ++module) {
        uint32_t mask = point ? (module == index / 24 ? 1UL << (index % 24) : 0) :
                               s_plan.masks[index][module];
        error = apply_module(module, s_plan.session, mask, false);
    }
    if (error != ESP_OK) clear_local(s_plan.modules);
    s_plan.touched = esp_timer_get_time();
    return error;
}

/* Select a half-open global port interval, including zero masks for other modules. */
static esp_err_t prepare_span(unsigned first, unsigned end)
{
    s_plan.selection_token = 0;
    s_plan.prepared_at = esp_timer_get_time();
    esp_err_t error = ch446_reset_all();
    for (unsigned module = 0; module < s_plan.modules && error == ESP_OK; ++module) {
        uint32_t mask = 0;
        for (unsigned local = 0; local < TOPOLOGY_GROUPS_PER_MODULE; ++local) {
            unsigned port = module * TOPOLOGY_GROUPS_PER_MODULE + local;
            if (first <= port && port < end) mask |= 1UL << local;
        }
        error = apply_module(module, s_plan.session, mask, false);
    }
    if (error != ESP_OK) clear_local(s_plan.modules);
    s_plan.touched = esp_timer_get_time();
    return error;
}

/* Versioned commands keep delivery cursors separate from physical scan coordinates. */
static bool execute_reliable(command_t *entry, unsigned argc, char **argv)
{
    const char *cmd = argv[0];
    bool run = !strcmp(cmd, "TOPO_RUN2"), point = !strcmp(cmd, "TOPO_POINT2");
    bool range = !strcmp(cmd, "TOPO_RANGE2");
    bool single = point || range;
    if (strcmp(cmd, "TOPO_OPEN") && strcmp(cmd, "TOPO_BEGIN2") &&
        strcmp(cmd, "TOPO_FETCH") && strcmp(cmd, "TOPO_ACK") &&
        strcmp(cmd, "TOPO_RESUME") && strcmp(cmd, "TOPO_PREP2") &&
        strcmp(cmd, "TOPO_PICK2") && strcmp(cmd, "TOPO_SPAN2") &&
        strcmp(cmd, "TOPO_VALIDATE") && !run && !single) return false;
    const char *problem = NULL;
    esp_err_t error = ESP_OK;
    uint32_t sid = 0, a = 0, b = 0, c = 0, d = 0, e = 0, f = 0;
    topology_journal_info_t info = {0};
    if (!s_journal_ready) problem = "CACHE_UNAVAILABLE";
    else if (argc < 2 || !parse_number(argv[1], 10, UINT32_MAX, &sid) || !sid) problem = "ARGUMENTS";
    else if (!strcmp(cmd, "TOPO_OPEN") && argc == 2) {
        if (!ch446_fixed_kelvin_enabled()) problem = "SOURCE_FIXED_ROUTE_REQUIRED";
        else if (s_plan.session || (s_job.active && sid != s_job.session)) problem = "BUSY";
        else {
            error = topology_journal_get_info(&info);
            if (error == ESP_OK) error = topology_journal_open(sid, entry->source);
            if (error == ESP_OK) {
                if (s_cache_session != sid) {
                    memset(&s_job, 0, sizeof(s_job));
                    s_cache_recovered = info.session == sid && info.recovered;
                }
                s_cache_session = sid;
                snprintf(s_cache_owner, sizeof(s_cache_owner), "%s", entry->source);
            }
        }
    } else if (!strcmp(cmd, "TOPO_BEGIN2") && argc == 4 &&
        parse_number(argv[2], 10, TOPOLOGY_MAX_MODULES, &a) && a &&
        parse_number(argv[3], 10, TOPOLOGY_MAX_ROUNDS, &b) && b) {
        if (ch446_fixed_kelvin_enabled()) problem = "SOURCE_CANNOT_BE_RECEIVER";
        else if (s_job.session) problem = "BUSY";
        else if (s_plan.session) {
            if (!s_plan.reliable || s_plan.session != sid || s_plan.modules != a || s_plan.rounds != b ||
                strcmp(entry->source, s_plan.owner)) problem = "PLAN_STATE";
        } else {
            error = clear_local(a);
            if (error == ESP_OK) {
                s_plan = (plan_t){.session = sid, .epoch = epoch(), .modules = a, .rounds = b,
                    .reliable = true, .cleared_epoch = epoch(), .touched = esp_timer_get_time()};
                snprintf(s_plan.owner, sizeof(s_plan.owner), "%s", entry->source);
            }
        }
    } else if ((!strcmp(cmd, "TOPO_PREP2") || !strcmp(cmd, "TOPO_PICK2")) && argc == 4 &&
        parse_number(argv[2], 10, TOPOLOGY_MAX_MODULES * 24 - 1, &a) &&
        parse_number(argv[3], 10, UINT32_MAX, &b) && b) {
        bool pick = !strcmp(cmd, "TOPO_PICK2");
        if (!s_plan.reliable || sid != s_plan.session || !s_plan.sealed ||
            a >= (pick ? s_plan.modules * 24 : s_plan.rounds) ||
            (s_plan.peer[0] && strcmp(entry->source, s_plan.peer))) problem = "PLAN_STATE";
        else if (s_plan.needs_resume || s_plan.epoch != epoch()) problem = "PLAN_PAUSED";
        else {
            snprintf(s_plan.peer, sizeof(s_plan.peer), "%s", entry->source);
            error = prepare_remote(a, pick);
            if (error == ESP_OK) {
                s_plan.selection_token = b;
                send_reply(entry->source, entry->request, "OK %s %" PRIu32, cmd, b);
                return true;
            }
        }
    } else if (!strcmp(cmd, "TOPO_SPAN2") && argc == 5 &&
        parse_number(argv[2], 10, TOPOLOGY_MAX_MODULES * 24 - 1, &a) &&
        parse_number(argv[3], 10, TOPOLOGY_MAX_MODULES * 24, &b) && a < b &&
        parse_number(argv[4], 10, UINT32_MAX, &c) && c) {
        if (!s_plan.reliable || sid != s_plan.session || !s_plan.sealed || b > s_plan.modules * 24 ||
            (s_plan.peer[0] && strcmp(entry->source, s_plan.peer))) problem = "PLAN_STATE";
        else if (s_plan.needs_resume || s_plan.epoch != epoch()) problem = "PLAN_PAUSED";
        else {
            snprintf(s_plan.peer, sizeof(s_plan.peer), "%s", entry->source);
            error = prepare_span(a, b);
            if (error == ESP_OK) {
                s_plan.selection_token = c;
                send_reply(entry->source, entry->request, "OK TOPO_SPAN2 %" PRIu32, c);
                return true;
            }
        }
    } else if (!strcmp(cmd, "TOPO_VALIDATE") && argc == 3 &&
        parse_number(argv[2], 10, UINT32_MAX, &a) && a) {
        if (!s_plan.reliable || sid != s_plan.session || strcmp(entry->source, s_plan.peer) ||
            s_plan.selection_token != a || s_plan.needs_resume || s_plan.epoch != epoch() ||
            esp_timer_get_time() - s_plan.prepared_at >= SELECTION_VALID_US) problem = "SELECTION_CHANGED";
    } else if (!strcmp(cmd, "TOPO_RESUME") && argc == 2) {
        if (s_plan.reliable && s_plan.session == sid && !strcmp(entry->source, s_plan.owner)) {
            s_plan.selection_token = 0;
            error = clear_local(s_plan.modules);
            if (error == ESP_OK) {
                s_plan.epoch = epoch();
                s_plan.cleared_epoch = s_plan.epoch;
                s_plan.needs_resume = false;
                s_plan.touched = esp_timer_get_time();
            }
        } else if (s_cache_session == sid && !strcmp(entry->source, s_cache_owner)) {
            if (s_cache_recovered) problem = "REBOOT_REQUIRES_NEW_SCAN";
            else {
                /* Resuming uploads must not leave the previous job's session,
                 * job_id or peer behind. That stale state is what must have been
                 * cleared by s_cache_session changing inside TOPO_OPEN; relying on
                 * it makes the next scan of the same session able to inherit it.
                 * A job still owned by this session is mid-scan and keeps its
                 * identity so that its samples are rejected only when the epoch
                 * really moved. */
                if (!s_job.active || s_job.session != sid) {
                    memset(&s_job, 0, sizeof(s_job));
                }
                if (s_job.reliable) {
                    s_job.resume_epoch = epoch();
                    ++s_job.resume_generation;
                }
            }
        } else problem = "SESSION_MISMATCH";
    } else if (!strcmp(cmd, "TOPO_FETCH") && argc == 4 &&
        parse_number(argv[2], 10, UINT32_MAX, &a) && a &&
        parse_number(argv[3], 10, 16, &b) && b) {
        error = topology_journal_get_info(&info);
        if (error == ESP_OK && (sid != info.session || strcmp(entry->source, info.owner))) problem = "SESSION_MISMATCH";
        else if (error == ESP_OK && a < info.first) problem = "ALREADY_ACKED";
        else if (error == ESP_OK && a > info.next) problem = "INVALID_SEQUENCE";
        else if (error == ESP_OK) {
            for (unsigned count = 0; count < b && a < info.next; ++count, ++a) {
                topology_journal_record_t record;
                error = topology_journal_read(a, &record);
                if (error != ESP_OK) break;
                error = send_reply(entry->source, entry->request,
                    "TOPO_DATA %" PRIu32 " %" PRIu32 " %" PRIu32 " %08" PRIx32 " %s",
                    record.session, record.sequence, record.job_id, record.crc, record.payload);
                if (error != ESP_OK) break;
            }
            if (error == ESP_OK) {
                const char *state = s_cache_recovered ? "RECOVERED" :
                    s_job.reliable && s_job.state ? s_job.state : "IDLE";
                const char *reason = s_job.reliable && s_job.pause_reason ? s_job.pause_reason : "NONE";
                unsigned used = info.capacity_records ?
                    (unsigned)((uint64_t)info.used_records * 100 / info.capacity_records) : 100;
                send_reply(entry->source, entry->request,
                    "OK TOPO_FETCH session=%" PRIu32 " first=%" PRIu32 " next=%" PRIu32 " ack=%" PRIu32
                    " used=%u job=%" PRIu32 " state=%s reason=%s high=70 low=50",
                    sid, info.first, info.next, info.ack, used, s_job.reliable ? s_job.job_id : 0, state, reason);
                return true;
            }
        }
    } else if (!strcmp(cmd, "TOPO_ACK") && argc == 4 &&
        parse_number(argv[2], 10, UINT32_MAX, &a) &&
        parse_number(argv[3], 16, UINT32_MAX, &b)) {
        error = topology_journal_get_info(&info);
        if (error == ESP_OK && (sid != info.session || strcmp(entry->source, info.owner))) problem = "SESSION_MISMATCH";
        else if (error == ESP_OK) error = topology_journal_ack(a, b);
    } else if ((run && argc == 7) || (point && argc == 8) || (range && argc == 9)) {
        if (!parse_number(argv[2], 10, UINT32_MAX, &a) || !a || strlen(argv[3]) >= ID_SIZE ||
            !strcmp(argv[3], WIFI_DEVICE_ID) ||
            !parse_number(argv[4], 10, TOPOLOGY_MAX_MODULES, &b) || !b ||
            !parse_number(argv[5], 10, single ? b * 24 - 1 : TOPOLOGY_MAX_ROUNDS, &c) || (!single && !c) ||
            !parse_number(argv[6], 10, single ? TOPOLOGY_MAX_MODULES * 24 - 1 : 5000, &d) ||
            (point && !parse_number(argv[7], 10, 5000, &e)) ||
            (range && (!parse_number(argv[7], 10, TOPOLOGY_MAX_MODULES * 24, &e) || e <= d ||
                       !parse_number(argv[8], 10, 5000, &f)))) problem = "ARGUMENTS";
        else if (sid != s_cache_session || strcmp(entry->source, s_cache_owner)) problem = "SESSION_MISMATCH";
        else if (s_cache_recovered) problem = "REBOOT_REQUIRES_NEW_SCAN";
        else if (s_plan.session || !ch446_fixed_kelvin_enabled()) problem = "SOURCE_FIXED_ROUTE_REQUIRED";
        else if (s_job.reliable && s_job.job_id == a) {
            if (s_job.session != sid || s_job.point != single || s_job.range != range || strcmp(s_job.peer, argv[3]) ||
                s_job.modules != b || s_job.rounds != (single ? 1 : c) ||
                s_job.source_port != (single ? c : 0) || s_job.destination_port != (single ? d : 0) ||
                s_job.range_end != (range ? e : 0) ||
                s_job.settle_ms != (range ? f : point ? e : d)) problem = "JOB_MISMATCH";
        } else if (s_job.active) problem = "BUSY";
        else if (a != (s_job.reliable ? s_job.job_id : 0) + 1) problem = "JOB_SEQUENCE";
        else {
            s_job = (job_t){.active = true, .point = single, .range = range, .reliable = true, .job_id = a,
                .epoch = epoch(), .resume_epoch = epoch(), .state = "RUNNING", .pause_reason = "NONE",
                .session = sid, .modules = b, .rounds = single ? 1 : c,
                .source_port = single ? c : 0, .destination_port = single ? d : 0,
                .range_end = range ? e : 0,
                .settle_ms = range ? f : point ? e : d, .touched = esp_timer_get_time()};
            snprintf(s_job.owner, sizeof(s_job.owner), "%s", entry->source);
            snprintf(s_job.request, sizeof(s_job.request), "%s", entry->request);
            snprintf(s_job.peer, sizeof(s_job.peer), "%s", argv[3]);
            portENTER_CRITICAL(&s_flags_lock); s_cancel = false; portEXIT_CRITICAL(&s_flags_lock);
            if (xTaskCreate(reliable_scan_task, "topo_cached", 8192, NULL, 4, NULL) != pdPASS) {
                s_job.active = false; s_job.state = "FAILED"; s_job.pause_reason = "TASK";
                error = ESP_ERR_NO_MEM;
            }
        }
    } else problem = "ARGUMENTS";
    if (problem) send_reply(entry->source, entry->request, "ERR %s %s", cmd, problem);
    else if (error != ESP_OK) send_reply(entry->source, entry->request, "ERR %s %s", cmd, esp_err_to_name(error));
    else send_reply(entry->source, entry->request, "OK %s", cmd);
    return true;
}

static void execute(command_t *entry)
{
    char *argv[9];
    unsigned argc = 0;
    char *save;
    for (char *p = strtok_r(entry->text, " ", &save); p; p = strtok_r(NULL, " ", &save)) {
        if (argc == 9) { send_reply(entry->source, entry->request, "ERR TOPO ARGUMENTS"); return; }
        argv[argc++] = p;
    }
    if (!argc) return;
    const char *cmd = argv[0];
    uint32_t a = 0, b = 0, c = 0, d = 0, e = 0;
    take();
    if (entry->epoch != epoch()) { give(); return; }
    if (execute_reliable(entry, argc, argv)) { give(); return; }
    const char *problem = NULL;
    esp_err_t error = ESP_OK;
    if (!strcmp(cmd, "TOPO_INFO") && argc == 1) {
        topology_journal_info_t info = {0};
        esp_err_t cache_error = s_journal_ready ? topology_journal_get_info(&info) : s_journal_error;
        send_reply(entry->source, entry->request,
            "OK TOPO_INFO role=MASTER capacity=%u configured=%u bus=1 route=%u reliable=1 cache_ready=%u cache_high=70 cache_low=50"
            " cache_session=%" PRIu32 " cache_ack=%" PRIu32 " cache_next=%" PRIu32 " plan_session=%" PRIu32
            " binary=1 cache_error=%s",
            TOPOLOGY_MAX_MODULES, TOPOLOGY_DEFAULT_MODULES, ch446_fixed_kelvin_enabled() ? 1 : 0,
            cache_error == ESP_OK ? 1 : 0, info.session, info.ack, info.next, s_plan.session, esp_err_to_name(cache_error));
        give(); return;
    } else if ((s_plan.session && !s_plan.reliable && s_plan.epoch != epoch()) ||
               (s_job.session && !s_job.reliable && s_job.epoch != epoch())) {
        problem = "RECOVERY_PENDING";
    } else if (!strcmp(cmd, "TOPO_DISCOVER") && argc == 2 &&
        parse_number(argv[1], 10, TOPOLOGY_MAX_MODULES, &a) && a) {
        if (s_job.session || s_plan.session) problem = "BUSY";
        else {
            s_selected_modules = a;
            uint32_t online = 0;
            for (unsigned i = 0; i < a; ++i) if (rs485_master_probe_module(i) == ESP_OK) online |= 1UL << i;
            send_reply(entry->source, entry->request, "OK TOPO_DISCOVER count=%" PRIu32 " online=%08" PRIx32, a, online);
            give(); return;
        }
    } else if (!strcmp(cmd, "TOPO_BEGIN") && argc == 4 &&
        parse_number(argv[1], 10, UINT32_MAX, &a) && a &&
        parse_number(argv[2], 10, TOPOLOGY_MAX_MODULES, &b) && b &&
        parse_number(argv[3], 10, TOPOLOGY_MAX_ROUNDS, &c) && c) {
        if (s_job.session || s_plan.session) problem = "BUSY";
        else if (ch446_fixed_kelvin_enabled()) problem = "SOURCE_CANNOT_BE_RECEIVER";
        else {
            error = clear_local(b);
            if (error == ESP_OK) {
                memset(&s_plan, 0, sizeof(s_plan));
                s_plan.session = a; s_plan.modules = b; s_plan.rounds = c;
                s_plan.epoch = entry->epoch;
                s_plan.touched = esp_timer_get_time();
                snprintf(s_plan.owner, sizeof(s_plan.owner), "%s", entry->source);
            }
        }
    } else if (!strcmp(cmd, "TOPO_MASK") && argc == 5 &&
        parse_number(argv[1], 10, UINT32_MAX, &a) &&
        parse_number(argv[2], 10, TOPOLOGY_MAX_ROUNDS - 1, &b) &&
        parse_number(argv[3], 10, TOPOLOGY_MAX_MODULES - 1, &c) &&
        parse_number(argv[4], 16, 0xFFFFFF, &d)) {
        if (!a || a != s_plan.session || strcmp(entry->source, s_plan.owner) ||
            b >= s_plan.rounds || c >= s_plan.modules ||
            (s_plan.sealed && (!s_plan.reliable || s_plan.masks[b][c] != d))) problem = "PLAN_STATE";
        else {
            s_plan.masks[b][c] = d; s_plan.loaded[b] |= 1U << c;
            s_plan.touched = esp_timer_get_time();
        }
    } else if (!strcmp(cmd, "TOPO_SEAL") && argc == 2 && parse_number(argv[1], 10, UINT32_MAX, &a)) {
        if (!a || a != s_plan.session || strcmp(entry->source, s_plan.owner)) problem = "PLAN_STATE";
        else {
            for (unsigned i = 0; i < s_plan.rounds; ++i)
                if (s_plan.loaded[i] != (1U << s_plan.modules) - 1) problem = "PLAN_INCOMPLETE";
            if (!problem) { s_plan.sealed = true; s_plan.touched = esp_timer_get_time(); }
        }
    } else if ((!strcmp(cmd, "TOPO_PREP") || !strcmp(cmd, "TOPO_PICK")) && argc == 3 &&
        parse_number(argv[1], 10, UINT32_MAX, &a) &&
        parse_number(argv[2], 10, TOPOLOGY_MAX_MODULES * 24 - 1, &b)) {
        bool point = !strcmp(cmd, "TOPO_PICK");
        if (s_plan.reliable && (s_plan.needs_resume || s_plan.epoch != epoch())) problem = "PLAN_PAUSED";
        else if (!a || a != s_plan.session || !s_plan.sealed || s_job.active ||
            b >= (point ? s_plan.modules * 24 : s_plan.rounds) ||
            (s_plan.peer[0] && strcmp(entry->source, s_plan.peer))) problem = "PLAN_STATE";
        else {
            snprintf(s_plan.peer, sizeof(s_plan.peer), "%s", entry->source);
            error = prepare_remote(b, point);
        }
    } else if ((!strcmp(cmd, "TOPO_CLEAR") || !strcmp(cmd, "TOPO_RESET")) && argc == 2 &&
        parse_number(argv[1], 10, UINT32_MAX, &a) && a) {
        if (s_job.active) problem = "BUSY_USE_ABORT";
        else if (s_job.session && (a != s_job.session || strcmp(entry->source, s_job.owner))) problem = "SESSION_MISMATCH";
        else if (s_plan.session && (a != s_plan.session ||
            (strcmp(entry->source, s_plan.owner) && strcmp(entry->source, s_plan.peer)))) problem = "SESSION_MISMATCH";
        else if (s_cache_session && (a != s_cache_session || strcmp(entry->source, s_cache_owner))) problem = "SESSION_MISMATCH";
        else {
            unsigned modules = s_plan.session ? s_plan.modules : s_job.modules;
            s_plan.selection_token = 0;
            error = clear_local(modules ? modules : s_selected_modules);
            if (!strcmp(cmd, "TOPO_RESET") && error == ESP_OK) {
                memset(&s_plan, 0, sizeof(s_plan));
                memset(&s_job, 0, sizeof(s_job));
                s_cache_session = 0;
                s_cache_owner[0] = '\0';
            }
            else s_plan.touched = esp_timer_get_time();
        }
    } else if (!strcmp(cmd, "TOPO_ABORT") && argc == 2 &&
        parse_number(argv[1], 10, UINT32_MAX, &a) && a) {
        if ((s_job.active && (a != s_job.session || strcmp(entry->source, s_job.owner))) ||
            (s_cache_session && (a != s_cache_session || strcmp(entry->source, s_cache_owner)))) problem = "SESSION_MISMATCH";
        else {
            portENTER_CRITICAL(&s_flags_lock); s_cancel = true; portEXIT_CRITICAL(&s_flags_lock);
        }
    } else if ((!strcmp(cmd, "TOPO_RUN") && argc == 6) || (!strcmp(cmd, "TOPO_POINT") && argc == 7)) {
        bool point = argc == 7;
        if (!parse_number(argv[1], 10, UINT32_MAX, &a) || !a || strlen(argv[2]) >= ID_SIZE ||
            !strcmp(argv[2], WIFI_DEVICE_ID) ||
            !parse_number(argv[3], 10, TOPOLOGY_MAX_MODULES, &b) || !b ||
            !parse_number(argv[4], 10, point ? b * 24 - 1 : TOPOLOGY_MAX_ROUNDS, &c) || (!point && !c) ||
            !parse_number(argv[5], 10, point ? TOPOLOGY_MAX_MODULES * 24 - 1 : 5000, &d) ||
            (point && !parse_number(argv[6], 10, 5000, &e))) problem = "ARGUMENTS";
        else if (s_job.active || s_plan.session || s_cache_session) problem = "BUSY";
        else if (s_job.session && (a != s_job.session || strcmp(entry->source, s_job.owner))) problem = "SESSION_MISMATCH";
        else if (!ch446_fixed_kelvin_enabled()) problem = "SOURCE_FIXED_ROUTE_REQUIRED";
        else {
            s_job = (job_t){.active = true, .point = point, .epoch = epoch(), .session = a,
                .modules = b, .rounds = point ? 1 : c, .source_port = point ? c : 0,
                .destination_port = point ? d : 0, .settle_ms = point ? e : d,
                .touched = esp_timer_get_time()};
            snprintf(s_job.owner, sizeof(s_job.owner), "%s", entry->source);
            snprintf(s_job.request, sizeof(s_job.request), "%s", entry->request);
            snprintf(s_job.peer, sizeof(s_job.peer), "%s", argv[2]);
            portENTER_CRITICAL(&s_flags_lock); s_cancel = false; portEXIT_CRITICAL(&s_flags_lock);
            if (xTaskCreate(scan_task, "topo_scan", 6144, NULL, 4, NULL) != pdPASS) {
                s_job.active = false; error = ESP_ERR_NO_MEM;
            }
        }
    } else problem = "ARGUMENTS_OR_COMMAND";
    if (problem) send_reply(entry->source, entry->request, "ERR %s %s", cmd, problem);
    else if (error != ESP_OK) send_reply(entry->source, entry->request, "ERR %s %s", cmd, esp_err_to_name(error));
    else send_reply(entry->source, entry->request, "OK %s", cmd);
    give();
}

static void control_task(void *argument)
{
    (void)argument;
    command_t entry;
    while (true) {
        if (xQueueReceive(s_commands, &entry, pdMS_TO_TICKS(100)) == pdTRUE) execute(&entry);
        take();
        if (s_plan.session && (s_plan.epoch != epoch() || esp_timer_get_time() - s_plan.touched > PLAN_TIMEOUT_US)) {
            if (s_plan.reliable) {
                s_plan.selection_token = 0;
                if ((!s_plan.needs_resume || s_plan.cleared_epoch != epoch()) && clear_local(s_plan.modules) == ESP_OK) {
                    s_plan.needs_resume = true;
                    s_plan.cleared_epoch = epoch();
                }
            } else if (clear_local(s_plan.modules) == ESP_OK) memset(&s_plan, 0, sizeof(s_plan));
        }
        if (s_job.session && !s_job.reliable && !s_job.active && (s_job.epoch != epoch() ||
            esp_timer_get_time() - s_job.touched > PLAN_TIMEOUT_US)) {
            if (clear_local(s_job.modules) == ESP_OK) memset(&s_job, 0, sizeof(s_job));
        }
        give();
    }
}

esp_err_t topology_scan_init(void)
{
    if (s_lock) return ESP_OK;
    s_lock = xSemaphoreCreateMutex();
    s_commands = xQueueCreate(8, sizeof(command_t));
    s_peer_replies = xQueueCreate(1, TEXT_SIZE);
    if (!s_lock || !s_commands || !s_peer_replies) return ESP_ERR_NO_MEM;
    s_journal_error = topology_journal_init();
    s_journal_ready = s_journal_error == ESP_OK;
    if (s_journal_ready) {
        topology_journal_info_t info;
        if (topology_journal_get_info(&info) == ESP_OK && info.session) {
            s_cache_session = info.session;
            s_cache_recovered = info.recovered;
            snprintf(s_cache_owner, sizeof(s_cache_owner), "%s", info.owner);
        }
    }
    return xTaskCreate(control_task, "topo_control", 6144, NULL, 4, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

esp_err_t topology_scan_submit(const char *command, const char *source, const char *request_id)
{
    if (!s_lock || !command || !source || !request_id || !*source || !*request_id ||
        strlen(command) >= 224 || strlen(source) >= ID_SIZE || strlen(request_id) >= ID_SIZE) return ESP_ERR_INVALID_ARG;
    command_t entry = {.epoch = epoch()};
    snprintf(entry.text, sizeof(entry.text), "%s", command);
    snprintf(entry.source, sizeof(entry.source), "%s", source);
    snprintf(entry.request, sizeof(entry.request), "%s", request_id);
    return xQueueSend(s_commands, &entry, 0) == pdTRUE ? ESP_OK : ESP_ERR_NO_MEM;
}

void topology_scan_feed_result(const char *sender, const char *destination,
                               const char *request_id, const char *payload)
{
    if (!s_lock || strcmp(destination, WIFI_DEVICE_ID)) return;
    take();
    if (s_wait_request[0] && !strcmp(sender, s_wait_peer) && !strcmp(request_id, s_wait_request)) {
        char text[TEXT_SIZE];
        if (strlen(payload) < sizeof(text)) {
            snprintf(text, sizeof(text), "%s", payload);
            xQueueSend(s_peer_replies, text, 0);
        }
    }
    give();
}

void topology_scan_disconnected(void)
{
    portENTER_CRITICAL(&s_flags_lock);
    ++s_epoch;
    portEXIT_CRITICAL(&s_flags_lock);
}

esp_err_t topology_scan_debug_lock(void)
{
    if (!s_lock || xSemaphoreTake(s_lock, 0) != pdTRUE) return ESP_ERR_INVALID_STATE;
    if (s_job.session || s_plan.session || s_cache_session) { give(); return ESP_ERR_INVALID_STATE; }
    return ESP_OK;
}

void topology_scan_debug_unlock(void) { give(); }
