#pragma once
#include <stdbool.h>
#include "esp_err.h"

/* Queued master scan execution; never wait for a peer from the TCP receive task. */
esp_err_t topology_scan_init(void);
esp_err_t topology_scan_submit(const char *command, const char *source, const char *request_id);
void topology_scan_feed_result(const char *sender, const char *destination,
                               const char *request_id, const char *payload);
void topology_scan_disconnected(void);

/* Hold this guard across legacy hardware commands to exclude scan ownership. */
esp_err_t topology_scan_debug_lock(void);
void topology_scan_debug_unlock(void);
