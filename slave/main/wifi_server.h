#pragma once

#include "esp_err.h"
#include "board_config.h"

#if __has_include("wifi_config.local.h")
#include "wifi_config.local.h"
#else
#include "wifi_config.example.h"
#endif
#define WIFI_SERVER_TCP_PORT      3333              //电脑路由服务器监听的 TCP 端口
#define WIFI_DEVICE_ID            BOARD_SLAVE_WIFI_DEVICE_ID //由本侧主机编号和从机编号生成 m1-s1 等名称
#define WIFI_NODE_ROLE            "SLAVE"           //上位机显示的设备角色

/*
 * 启动 Wi-Fi STA，并创建自动连接 DHCP 默认网关上电脑服务器的 FreeRTOS 任务。
 * 注册后任务每 2 秒发送一次 PING，供服务器淘汰断电或掉网的陈旧连接。
 * 该寻址方式用于电脑自身开启热点、电脑同时充当默认网关的部署方式。
 * 应在 app_main() 初始化阶段调用，不应在 ISR 或事件回调中调用。
 */
esp_err_t wifi_server_start(void);

/*
 * 向已经在电脑服务器注册的目标设备发送一条带请求 ID 的载荷。
 * 只能在 STA/TCP 已连接后从普通任务调用；返回 ESP_OK 只表示写入本地 TCP 成功。
 */
esp_err_t wifi_server_send_request(const char *target_id,
                                   const char *request_id,
                                   const char *payload);
