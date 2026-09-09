#include "esp_err.h"
#include "esp_log.h"

#include "board_config.h"
#include "ch446.h"
#include "rs485_slave.h"
#include "wifi_server.h"

static const char *TAG = "cable_tester";

void app_main(void)
{
    ESP_LOGI(TAG, "ESP32-S3 cable tester firmware started");
    ESP_LOGI(TAG, "Slave identity: WiFi=%s enabled=%u RS485=0x%02X",
             WIFI_DEVICE_ID, (unsigned)BOARD_SLAVE_WIFI_DEBUG_ENABLED,
             (unsigned)BOARD_RS485_NODE_ID);

    ESP_ERROR_CHECK(ch446_init());
    ESP_ERROR_CHECK(rs485_slave_start(BOARD_RS485_NODE_ID));
#if BOARD_SLAVE_WIFI_DEBUG_ENABLED
    ESP_ERROR_CHECK(wifi_server_start());
#else
    ESP_LOGI(TAG, "Slave Wi-Fi disabled; RS485 address=0x%02X",
             BOARD_RS485_NODE_ID);
#endif
}
