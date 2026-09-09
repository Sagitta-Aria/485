#include "ch446.h"

#include <stddef.h>
#include <string.h>

#include "board_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define CH446_PULSE_DELAY_US 1U

typedef struct {
    gpio_num_t rst;
    gpio_num_t dat;
    gpio_num_t csck;
    gpio_num_t stb;
} ch446_pins_t;

static const char *TAG = "ch446_master";
static const ch446_pins_t s_pins = {
    .rst = BOARD_CH446_U1_RST_GPIO,
    .dat = BOARD_CH446_U1_DAT_GPIO,
    .csck = BOARD_CH446_U1_CSCK_GPIO,
    .stb = BOARD_CH446_U1_STB_GPIO,
};
static bool s_initialized;
static bool s_fixed_kelvin;
/* Matrix writes are timing-sensitive and may be requested by more than one task. */
static SemaphoreHandle_t s_matrix_mutex;
static ch446_matrix_status_t s_status;

static esp_err_t ch446_take(void)
{
    if (s_matrix_mutex == NULL ||
        xSemaphoreTakeRecursive(s_matrix_mutex, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

static void ch446_give(void)
{
    xSemaphoreGiveRecursive(s_matrix_mutex);
}

static void ch446_update_status(ch446_chip_t chip,
                                uint8_t x,
                                uint8_t y,
                                bool closed)
{
    const size_t bit_index = (size_t)y * CH446_X_COUNT + x;
    const size_t byte_index = bit_index / 8U;
    const uint8_t bit_mask = (uint8_t)(1U << (bit_index % 8U));
    if (closed) {
        s_status.chip[chip][byte_index] |= bit_mask;
    } else {
        s_status.chip[chip][byte_index] &= (uint8_t)~bit_mask;
    }
}

static esp_err_t ch446_encode_address(uint8_t x, uint8_t y, uint8_t *address)
{
    if (address == NULL || x >= CH446_X_COUNT || y >= CH446_Y_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    if (y < 4U) {
        *address = (uint8_t)((y << 5U) | x);
    } else {
        *address = (uint8_t)(((x / 6U) << 5U) | 0x18U | (x % 6U));
    }
    return ESP_OK;
}

static bool ch446_port_is_valid(ch446_port_t port)
{
    return port.bank == CH446_BANK_S1 && port.x < CH446_X_COUNT;
}

esp_err_t ch446_reset_all(void)
{
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    if (!s_initialized) {
        ch446_give();
        return ESP_ERR_INVALID_STATE;
    }
    if (s_fixed_kelvin) {
        /* Keep the four instrument leads continuously connected, even during RESET. */
        for (uint8_t y = 0; y < CH446_Y_COUNT; ++y) {
            for (uint8_t x = 0; x < CH446_X_COUNT; ++x) {
                size_t bit = y * CH446_X_COUNT + x;
                if (!(x < 4 && x == y) && (s_status.chip[0][bit / 8] & (1U << (bit % 8)))) {
                    esp_err_t error = ch446_set_crosspoint(CH446_CHIP_U1, x, y, false);
                    if (error != ESP_OK) { ch446_give(); return error; }
                }
            }
        }
        static const uint8_t order[] = {2, 1, 0, 3};
        for (size_t i = 0; i < sizeof(order); ++i) {
            esp_err_t error = ch446_set_crosspoint(CH446_CHIP_U1, order[i], order[i], true);
            if (error != ESP_OK) { ch446_give(); return error; }
        }
    } else {
        gpio_set_level(s_pins.rst, 1);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
        gpio_set_level(s_pins.rst, 0);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
        memset(&s_status, 0, sizeof(s_status));
    }
    ch446_give();
    return ESP_OK;
}

esp_err_t ch446_set_fixed_kelvin(bool enabled)
{
    esp_err_t error = ch446_take();
    if (error != ESP_OK) return error;
    s_fixed_kelvin = enabled;
    error = ch446_reset_all();
    ch446_give();
    return error;
}

bool ch446_fixed_kelvin_enabled(void)
{
    return s_fixed_kelvin;
}

esp_err_t ch446_init(void)
{
    if (s_matrix_mutex == NULL) {
        s_matrix_mutex = xSemaphoreCreateRecursiveMutex();
        if (s_matrix_mutex == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    if (s_initialized) {
        esp_err_t error = ch446_reset_all();
        ch446_give();
        return error;
    }
    const uint64_t output_mask =
        (1ULL << BOARD_CH446_U1_RST_GPIO) |
        (1ULL << BOARD_CH446_U1_DAT_GPIO) |
        (1ULL << BOARD_CH446_U1_CSCK_GPIO) |
        (1ULL << BOARD_CH446_U1_STB_GPIO);
    const gpio_config_t config = {
        .pin_bit_mask = output_mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t error = gpio_config(&config);
    if (error != ESP_OK) {
        ch446_give();
        return error;
    }
    gpio_set_level(s_pins.rst, 0);
    gpio_set_level(s_pins.dat, 0);
    gpio_set_level(s_pins.csck, 0);
    gpio_set_level(s_pins.stb, 0);
    memset(&s_status, 0, sizeof(s_status));
    s_initialized = true;
    error = ch446_reset_all();
    if (error != ESP_OK) {
        s_initialized = false;
        ch446_give();
        return error;
    }
    ESP_LOGI(TAG, "single U1/S1 matrix initialized; all crosspoints open");
    ch446_give();
    return ESP_OK;
}

esp_err_t ch446_set_crosspoint(ch446_chip_t chip,
                               uint8_t x,
                               uint8_t y,
                               bool closed)
{
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    if (!s_initialized || chip != CH446_CHIP_U1) {
        ch446_give();
        return ESP_ERR_INVALID_ARG;
    }
    /* A fixed instrument X must never bridge its Y bus to another bus. */
    if (s_fixed_kelvin && x < 4 &&
        ((x == y && !closed) || (x != y && closed))) {
        ch446_give();
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t address = 0U;
    esp_err_t error = ch446_encode_address(x, y, &address);
    if (error != ESP_OK) {
        ch446_give();
        return error;
    }
    gpio_set_level(s_pins.stb, 0);
    gpio_set_level(s_pins.csck, 0);
    for (int bit = 6; bit >= 0; bit--) {
        gpio_set_level(s_pins.dat, (address >> bit) & 1U);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
        gpio_set_level(s_pins.csck, 1);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
        gpio_set_level(s_pins.csck, 0);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
    }
    gpio_set_level(s_pins.dat, closed ? 1 : 0);
    esp_rom_delay_us(CH446_PULSE_DELAY_US);
    gpio_set_level(s_pins.stb, 1);
    esp_rom_delay_us(CH446_PULSE_DELAY_US);
    gpio_set_level(s_pins.stb, 0);
    esp_rom_delay_us(CH446_PULSE_DELAY_US);
    gpio_set_level(s_pins.dat, 0);
    ch446_update_status(chip, x, y, closed);
    ch446_give();
    return ESP_OK;
}

esp_err_t ch446_set_port_bus(ch446_port_t port,
                             ch446_bus_t bus,
                             bool closed)
{
    if (!ch446_port_is_valid(port) || bus >= CH446_BUS_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    esp_err_t error = ch446_set_crosspoint(CH446_CHIP_U1, port.x, (uint8_t)bus, closed);
    ch446_give();
    return error;
}

esp_err_t ch446_connect_kelvin_pair(ch446_port_t positive_port,
                                    ch446_port_t negative_port)
{
    if (!ch446_port_is_valid(positive_port) ||
        !ch446_port_is_valid(negative_port) ||
        positive_port.x == negative_port.x) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    /* Validate both ends before RESET or any partial closure changes the path. */
    if (s_fixed_kelvin && (positive_port.x < 4 || negative_port.x < 4)) {
        ch446_give();
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t error = ch446_reset_all();
    if (error != ESP_OK) {
        ch446_give();
        return error;
    }
    const struct {
        ch446_port_t port;
        ch446_bus_t bus;
    } steps[] = {
        {positive_port, CH446_BUS_V_PLUS},
        {negative_port, CH446_BUS_V_MINUS},
        {negative_port, CH446_BUS_I_MINUS},
        {positive_port, CH446_BUS_I_PLUS},
    };
    for (size_t index = 0; index < sizeof(steps) / sizeof(steps[0]); index++) {
        error = ch446_set_port_bus(steps[index].port, steps[index].bus, true);
        if (error != ESP_OK) {
            ch446_reset_all();
            ch446_give();
            return error;
        }
    }
    ch446_give();
    return ESP_OK;
}

esp_err_t ch446_get_status(ch446_matrix_status_t *status)
{
    if (status == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    if (!s_initialized) {
        ch446_give();
        return ESP_ERR_INVALID_STATE;
    }
    memcpy(status, &s_status, sizeof(*status));
    ch446_give();
    return ESP_OK;
}
