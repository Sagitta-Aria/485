#include "ch446.h"

#include <stddef.h>
#include <string.h>

#include "board_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define CH446_PULSE_DELAY_US 1U   //脉冲延迟时间（微秒）

typedef struct {
    gpio_num_t rst;  //复位引脚
    gpio_num_t dat;  //数据引脚
    gpio_num_t csck; //时钟引脚
    gpio_num_t stb;  //选通信号引脚
} ch446_pins_t;

typedef struct {
    ch446_port_t port;  //丝印端口（S1_Xn 或 S2_Xn）
    ch446_bus_t bus;  //连接的总线类型
} ch446_path_step_t;

static const char *TAG = "ch446";

static const ch446_pins_t s_chip_pins[] = {  //引脚配置数组，包含两个 CH446 芯片的引脚配置
    [CH446_CHIP_U1] = {
        .rst = BOARD_CH446_U1_RST_GPIO,
        .dat = BOARD_CH446_U1_DAT_GPIO,
        .csck = BOARD_CH446_U1_CSCK_GPIO,
        .stb = BOARD_CH446_U1_STB_GPIO,
    },
    [CH446_CHIP_U2] = {
        .rst = BOARD_CH446_U2_RST_GPIO,
        .dat = BOARD_CH446_U2_DAT_GPIO,
        .csck = BOARD_CH446_U2_CSCK_GPIO,
        .stb = BOARD_CH446_U2_STB_GPIO,
    },
};

static bool s_initialized;  //模块初始化标志，表示 CH446 模块是否已初始化
/* UART and WiFi tasks can both change the matrix; serialize the GPIO waveform. */
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

static bool ch446_chip_is_valid(ch446_chip_t chip)  //检查软件编号是否正确
{
    return chip == CH446_CHIP_U1 || chip == CH446_CHIP_U2;  //如果软件编号是 U1 或 U2，返回 true，否则返回 false
}

static esp_err_t ch446_encode_address(uint8_t x, uint8_t y, uint8_t *address)  //将 x、y 坐标编码为 CH446 地址
{
    if (address == NULL || x >= CH446_X_COUNT || y >= CH446_Y_COUNT) {
        return ESP_ERR_INVALID_ARG;  //如果传入的地址指针为 NULL，或者 x、y 坐标超出范围，返回无效参数错误
    }

    if (y < 4U) {
        *address = (uint8_t)((y << 5U) | x);
    } else {
        /* CH446X places Y4 at 0x18..0x1D, 0x38..0x3D, ... . */
        *address = (uint8_t)(((x / 6U) << 5U) | 0x18U | (x % 6U));
    }

    return ESP_OK;
}

static bool ch446_port_is_valid(ch446_port_t port)
{
    return port.x < CH446_X_COUNT &&
           (port.bank == CH446_BANK_S1 || port.bank == CH446_BANK_S2);
}

static ch446_chip_t ch446_port_to_chip(ch446_port_t port)
{
    return port.bank == CH446_BANK_S1 ? CH446_CHIP_U1 : CH446_CHIP_U2;
}

esp_err_t ch446_reset_all(void)  //复位所有 CH446 芯片
{
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    if (!s_initialized) {
        ch446_give();
        return ESP_ERR_INVALID_STATE;  //如果模块未初始化，返回无效状态错误
    }

    gpio_set_level(s_chip_pins[CH446_CHIP_U1].rst, 1);  //设置 U1 芯片的复位引脚为高电平，开始复位
    gpio_set_level(s_chip_pins[CH446_CHIP_U2].rst, 1);  //设置 U2 芯片的复位引脚为高电平，开始复位
    esp_rom_delay_us(CH446_PULSE_DELAY_US);
    gpio_set_level(s_chip_pins[CH446_CHIP_U1].rst, 0);  //设置 U1 芯片的复位引脚为低电平，结束复位
    gpio_set_level(s_chip_pins[CH446_CHIP_U2].rst, 0);  //设置 U2 芯片的复位引脚为低电平，结束复位
    esp_rom_delay_us(CH446_PULSE_DELAY_US);

    memset(&s_status, 0, sizeof(s_status));

    ch446_give();
    return ESP_OK;
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
        return error;  //如果模块已初始化，调用复位函数复位所有 CH446 芯片
    }

    const uint64_t output_mask =  //定义一个 64 位的输出掩码，用于配置 GPIO 引脚为输出模式
        (1ULL << BOARD_CH446_U1_RST_GPIO) |   //设置 U1 芯片的复位引脚为输出模式
        (1ULL << BOARD_CH446_U1_DAT_GPIO) |   //设置 U1 芯片的数据引脚为输出模式
        (1ULL << BOARD_CH446_U1_CSCK_GPIO) |  //设置 U1 芯片的时钟引脚为输出模式
        (1ULL << BOARD_CH446_U1_STB_GPIO) |   //设置 U1 芯片的选通信号引脚为输出模式
        (1ULL << BOARD_CH446_U2_RST_GPIO) |   //设置 U2 芯片的复位引脚为输出模式
        (1ULL << BOARD_CH446_U2_DAT_GPIO) |   //设置 U2 芯片的数据引脚为输出模式
        (1ULL << BOARD_CH446_U2_CSCK_GPIO) |  //设置 U2 芯片的时钟引脚为输出模式
        (1ULL << BOARD_CH446_U2_STB_GPIO);    //设置 U2 芯片的选通信号引脚为输出模式


    for (size_t chip = 0; chip < sizeof(s_chip_pins) / sizeof(s_chip_pins[0]); chip++) {  //循环遍历所有 CH446 芯片的引脚配置，初始化每个芯片的 GPIO 引脚为低电平
        gpio_set_level(s_chip_pins[chip].rst, 0);
        gpio_set_level(s_chip_pins[chip].dat, 0);
        gpio_set_level(s_chip_pins[chip].csck, 0);
        gpio_set_level(s_chip_pins[chip].stb, 0);
    }

    const gpio_config_t io_config = {
        .pin_bit_mask = output_mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    esp_err_t error = gpio_config(&io_config);
    if (error != ESP_OK) {
        ch446_give();
        return error;
    }

    memset(&s_status, 0, sizeof(s_status));
    s_initialized = true;
    error = ch446_reset_all();
    if (error != ESP_OK) {
        s_initialized = false;
        ch446_give();
        return error;
    }

    ESP_LOGI(TAG, "U1 and U2 initialized; all crosspoints open");
    ch446_give();
    return ESP_OK;
}

esp_err_t ch446_set_crosspoint(  //设置交叉点
    ch446_chip_t chip,
    uint8_t x,
    uint8_t y,
    bool closed)
{
    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }
    if (!s_initialized) {
        ch446_give();
        return ESP_ERR_INVALID_STATE;
    }
    if (!ch446_chip_is_valid(chip)) {
        ch446_give();
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t address = 0;
    esp_err_t error = ch446_encode_address(x, y, &address);
    if (error != ESP_OK) {
        ch446_give();
        return error;
    }

    const ch446_pins_t *pins = &s_chip_pins[chip];
    gpio_set_level(pins->stb, 0);
    gpio_set_level(pins->csck, 0);

    for (int bit = 6; bit >= 0; bit--) {  //发送xy坐标码
        gpio_set_level(pins->dat, (address >> bit) & 1U);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
        gpio_set_level(pins->csck, 1);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
        gpio_set_level(pins->csck, 0);
        esp_rom_delay_us(CH446_PULSE_DELAY_US);
    }

    gpio_set_level(pins->dat, closed ? 1 : 0);
    esp_rom_delay_us(CH446_PULSE_DELAY_US);
    gpio_set_level(pins->stb, 1);
    esp_rom_delay_us(CH446_PULSE_DELAY_US);
    gpio_set_level(pins->stb, 0);
    esp_rom_delay_us(CH446_PULSE_DELAY_US);
    gpio_set_level(pins->dat, 0);
    ch446_update_status(chip, x, y, closed);

    ch446_give();
    return ESP_OK;
}

esp_err_t ch446_set_port_bus(
    ch446_port_t port,
    ch446_bus_t bus,
    bool closed)
{
    if (!ch446_port_is_valid(port) ||
        bus < CH446_BUS_I_MINUS || bus >= CH446_BUS_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }

    esp_err_t error = ch446_set_crosspoint(
        ch446_port_to_chip(port),
        port.x,
        (uint8_t)bus,
        closed);
    ch446_give();
    return error;
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

static esp_err_t ch446_apply_path(
    const ch446_path_step_t *steps,
    size_t step_count)
{
    if (steps == NULL || step_count == 0U) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t error = ch446_reset_all();
    if (error != ESP_OK) {
        return error;
    }

    for (size_t step = 0; step < step_count; step++) {
        error = ch446_set_port_bus(
            steps[step].port,
            steps[step].bus,
            true);
        if (error != ESP_OK) {
            ch446_reset_all();
            return error;
        }
    }

    return ESP_OK;
}

esp_err_t ch446_connect_kelvin_pair(  //开尔文探头连接
    ch446_port_t positive_port,  //正端口
    ch446_port_t negative_port)  //负端口
{
    if (!ch446_port_is_valid(positive_port) ||
        !ch446_port_is_valid(negative_port) ||
        (positive_port.bank == negative_port.bank &&
         positive_port.x == negative_port.x)) {
        return ESP_ERR_INVALID_ARG;
    }  //检查正端口和负端口是否有效，并确保它们不在同一个芯片的同一个位置上

    esp_err_t lock_error = ch446_take();
    if (lock_error != ESP_OK) {
        return lock_error;
    }

    const ch446_path_step_t steps[] = {  //定义开尔文四线测量路径
        {.port = positive_port, .bus = CH446_BUS_V_PLUS},
        {.port = negative_port, .bus = CH446_BUS_V_MINUS},
        {.port = negative_port, .bus = CH446_BUS_I_MINUS},
        {.port = positive_port, .bus = CH446_BUS_I_PLUS},
    };

    esp_err_t error = ch446_apply_path(steps, sizeof(steps) / sizeof(steps[0]));
    ch446_give();
    return error;  //应用普通检查的四线测量路径
}

esp_err_t ch446_apply_kelvin_mask(uint32_t mask, bool positive)
{
    if ((mask & ~0x00FFFFFFUL) != 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t error = ch446_take();
    if (error != ESP_OK) {
        return error;
    }
    error = ch446_reset_all();
    /* Finish every voltage contact before any current contact. */
    for (unsigned current = 0U; current < 2U && error == ESP_OK; current++) {
        for (unsigned group = 0U; group < 24U && error == ESP_OK; group++) {
            if ((mask & (1UL << group)) == 0U) {
                continue;
            }
            const ch446_port_t port = {
                .bank = group < 12U ? CH446_BANK_S1 : CH446_BANK_S2,
                .x = (uint8_t)((group % 12U) * 2U + (current == 0U ? 1U : 0U)),
            };
            ch446_bus_t bus = positive ? CH446_BUS_V_PLUS : CH446_BUS_V_MINUS;
            if (current != 0U) {
                bus = positive ? CH446_BUS_I_PLUS : CH446_BUS_I_MINUS;
            }
            error = ch446_set_port_bus(port, bus, true);
        }
    }
    if (error != ESP_OK) {
        ch446_reset_all();
    }
    ch446_give();
    return error;
}
