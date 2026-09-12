#include "esp_err.h"
#include "esp_log.h"
#include <string.h>

#include "ch446.h"
#include "wifi_server.h"
#include "xd31h.h"
#include "rs485_master.h"
#include "topology_scan.h"

static const char *TAG = "cable_tester";

void app_main(void)
{
    ESP_LOGI(TAG, "ESP32-S3 cable tester firmware started");

    ESP_ERROR_CHECK(ch446_init());
    ESP_ERROR_CHECK(ch446_set_fixed_kelvin(strcmp(WIFI_DEVICE_ID, "master1") == 0));
    ESP_ERROR_CHECK(xd31h_init());
    ESP_ERROR_CHECK(rs485_master_start());
    ESP_ERROR_CHECK(topology_scan_init());
    ESP_ERROR_CHECK(wifi_server_start());
}
