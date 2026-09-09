idf_component_register(SRCS "app_main.c" "ch446_master.c" "xd31h.c"
                              "rs485_bus.c" "rs485_master.c" "wifi_server.c" "topology_scan.c" "topology_journal.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_driver_gpio esp_driver_uart esp_event
                                esp_netif esp_rom esp_wifi esp_timer esp_partition lwip nvs_flash)
