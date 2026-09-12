/* Exercise the real master driver while recording hardware-reset pulses. */
#include "../../master/main/ch446_master.c"

#define CHECK(condition) do { if (!(condition)) return __LINE__; } while (0)
#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

struct fake_mutex { unsigned depth; };
static struct fake_mutex mutex;
static unsigned reset_pulses;

SemaphoreHandle_t xSemaphoreCreateRecursiveMutex(void) { mutex.depth = 0; return &mutex; }
BaseType_t xSemaphoreTakeRecursive(SemaphoreHandle_t semaphore, TickType_t wait)
{
    (void)wait;
    ++semaphore->depth;
    return pdTRUE;
}
void xSemaphoreGiveRecursive(SemaphoreHandle_t semaphore) { --semaphore->depth; }
esp_err_t gpio_config(const gpio_config_t *config) { (void)config; return ESP_OK; }
esp_err_t gpio_set_level(gpio_num_t gpio, unsigned level)
{
    if (gpio == BOARD_CH446_U1_RST_GPIO && level) ++reset_pulses;
    return ESP_OK;
}
void esp_rom_delay_us(unsigned delay) { (void)delay; }

static bool connected(unsigned x, unsigned y)
{
    unsigned bit = y * CH446_X_COUNT + x;
    return (s_status.chip[0][bit / 8] & (1U << (bit % 8))) != 0;
}

static esp_err_t initialize(bool fixed)
{
    s_initialized = false;
    s_fixed_kelvin = false;
    s_matrix_mutex = NULL;
    reset_pulses = 0;
    esp_err_t error = ch446_init();
    if (error == ESP_OK) error = ch446_set_fixed_kelvin(fixed);
    return error;
}

EXPORT int topology_test_fixed_route_reset(void)
{
    CHECK(initialize(true) == ESP_OK);
    CHECK(ch446_fixed_kelvin_enabled());
    unsigned before = reset_pulses;
    for (unsigned line = 0; line < 4; ++line) CHECK(connected(line, line));
    CHECK(ch446_set_crosspoint(CH446_CHIP_U1, 8, 4, true) == ESP_OK);
    CHECK(connected(8, 4));
    CHECK(ch446_reset_all() == ESP_OK);
    CHECK(reset_pulses == before);
    CHECK(!connected(8, 4));
    for (unsigned line = 0; line < 4; ++line) CHECK(connected(line, line));
    CHECK(mutex.depth == 0);
    return 0;
}

EXPORT int topology_test_fixed_route_rejects_open(void)
{
    CHECK(initialize(true) == ESP_OK);
    for (unsigned line = 0; line < 4; ++line) {
        CHECK(ch446_set_crosspoint(CH446_CHIP_U1, line, line, false) != ESP_OK);
        CHECK(connected(line, line));
    }
    CHECK(mutex.depth == 0);
    return 0;
}

EXPORT int topology_test_unfixed_master_clears_all(void)
{
    CHECK(initialize(false) == ESP_OK);
    CHECK(!ch446_fixed_kelvin_enabled());
    CHECK(ch446_set_crosspoint(CH446_CHIP_U1, 0, 0, true) == ESP_OK);
    unsigned before = reset_pulses;
    CHECK(ch446_reset_all() == ESP_OK);
    CHECK(reset_pulses == before + 1);
    for (unsigned y = 0; y < CH446_Y_COUNT; ++y)
        for (unsigned x = 0; x < CH446_X_COUNT; ++x) CHECK(!connected(x, y));
    CHECK(mutex.depth == 0);
    return 0;
}

EXPORT int topology_test_fixed_route_rejects_bus_bridges(void)
{
    CHECK(initialize(true) == ESP_OK);
    ch446_matrix_status_t before = s_status;
    for (unsigned x = 0; x < 4; ++x) {
        for (unsigned y = 0; y < CH446_Y_COUNT; ++y) {
            if (x == y) continue;
            CHECK(ch446_set_crosspoint(CH446_CHIP_U1, x, y, true) == ESP_ERR_INVALID_STATE);
            CHECK(memcmp(&before, &s_status, sizeof(before)) == 0);
            CHECK(ch446_set_crosspoint(CH446_CHIP_U1, x, y, false) == ESP_OK);
        }
    }
    CHECK(mutex.depth == 0);
    return 0;
}

EXPORT int topology_test_fixed_route_connect_rejects_before_reset(void)
{
    CHECK(initialize(true) == ESP_OK);
    CHECK(ch446_set_crosspoint(CH446_CHIP_U1, 8, 4, true) == ESP_OK);
    ch446_matrix_status_t before = s_status;
    ch446_port_t selectable = {CH446_BANK_S1, 5};
    for (unsigned x = 0; x < 4; ++x) {
        ch446_port_t reserved = {CH446_BANK_S1, x};
        CHECK(ch446_connect_kelvin_pair(selectable, reserved) == ESP_ERR_INVALID_STATE);
        CHECK(memcmp(&before, &s_status, sizeof(before)) == 0);
        CHECK(ch446_connect_kelvin_pair(reserved, selectable) == ESP_ERR_INVALID_STATE);
        CHECK(memcmp(&before, &s_status, sizeof(before)) == 0);
    }
    CHECK(mutex.depth == 0);
    return 0;
}

EXPORT int topology_test_fixed_route_cleans_legacy_bridges(void)
{
    CHECK(initialize(true) == ESP_OK);
    /* Replay the software shadow captured from the affected running firmware. */
    ch446_update_status(CH446_CHIP_U1, 1, 2, true);
    ch446_update_status(CH446_CHIP_U1, 3, 1, true);
    ch446_update_status(CH446_CHIP_U1, 2, 0, true);
    ch446_update_status(CH446_CHIP_U1, 0, 3, true);
    CHECK(ch446_reset_all() == ESP_OK);
    for (unsigned y = 0; y < CH446_Y_COUNT; ++y)
        for (unsigned x = 0; x < CH446_X_COUNT; ++x)
            CHECK(connected(x, y) == (x < 4 && x == y));
    CHECK(mutex.depth == 0);
    return 0;
}
