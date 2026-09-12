idf_component_register(SRCS "app_main.c" "ch446.c" "rs485_bus.c"
                              "rs485_slave.c" "wifi_server.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_driver_gpio esp_driver_uart esp_event
                                esp_netif esp_rom esp_wifi lwip nvs_flash)
