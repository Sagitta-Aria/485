#pragma once
#include "esp_err.h"
typedef int gpio_num_t;
typedef struct {
    uint64_t pin_bit_mask;
    int mode, pull_up_en, pull_down_en, intr_type;
} gpio_config_t;
#define GPIO_NUM_2 2
#define GPIO_NUM_4 4
#define GPIO_NUM_5 5
#define GPIO_NUM_6 6
#define GPIO_NUM_7 7
#define GPIO_NUM_17 17
#define GPIO_NUM_18 18
#define GPIO_NUM_39 39
#define GPIO_NUM_40 40
#define GPIO_MODE_OUTPUT 1
#define GPIO_PULLUP_DISABLE 0
#define GPIO_PULLDOWN_DISABLE 0
#define GPIO_INTR_DISABLE 0
esp_err_t gpio_config(const gpio_config_t *config);
esp_err_t gpio_set_level(gpio_num_t gpio, unsigned level);
