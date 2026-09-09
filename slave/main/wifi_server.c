#include "wifi_server.h"

#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "ch446.h"
#include "rs485_slave.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

#define WIFI_SERVER_RX_BUFFER_SIZE       256U   // TCP 单行接收、命令和回复缓冲区大小
#define WIFI_SERVER_TASK_STACK_SIZE      6144U  // Wi-Fi/TCP 客户端任务的栈空间大小
#define WIFI_SERVER_TASK_PRIORITY        5U     // FreeRTOS 任务优先级，数字越大优先级越高
#define WIFI_SERVER_ID_SIZE              32U    // 设备 ID 和请求 ID 的最大缓冲区大小（含 '\0'）
#define WIFI_SERVER_WIFI_CONNECTED_BIT   BIT0   // 事件组中表示 STA 已获得 IP 的状态位
#define WIFI_SERVER_HEARTBEAT_INTERVAL_MS 2000U // 空闲时也定期证明本 TCP 会话仍然存活

static const char *TAG = "wifi_client";  // ESP-IDF 日志标签
static bool s_started;                    // 防止 Wi-Fi 客户端被重复初始化
static int s_connection_socket = -1;      // 当前连接电脑路由服务器的 socket；-1 表示未连接
static EventGroupHandle_t s_wifi_event_group; // 在 Wi-Fi 事件回调和 TCP 任务之间同步联网状态
static SemaphoreHandle_t s_socket_mutex;       // 防止多个任务同时向同一个 TCP socket 写数据
static esp_ip4_addr_t s_server_address;        // DHCP 默认网关，即电脑热点服务器地址
static bool s_result_context_active;           // 当前本地回复是否需要封装成 RESULT 路由帧
static char s_result_source[WIFI_SERVER_ID_SIZE];     // 当前请求的源设备 ID
static char s_result_request_id[WIFI_SERVER_ID_SIZE]; // 当前请求的关联 ID

typedef struct {
    ch446_port_t positive_port; // 四线测量的正端口
    ch446_port_t negative_port; // 四线测量的负端口
} wifi_server_port_pair_t;

/*
 * 检查设备 ID 或请求 ID 是否能安全地作为协议中的单个字段使用。
 * 用于 SEND/FROM/RESULT 的 ID 字段；不能用于包含空格的命令载荷。
 * 本函数只读字符串，不修改网络状态。
 */
static bool wifi_server_valid_token(const char *value)
{
    if (value == NULL || *value == '\0' ||
        strlen(value) >= WIFI_SERVER_ID_SIZE) {
        return false;
    }
    for (const char *cursor = value; *cursor != '\0'; cursor++) {
        if (isspace((unsigned char)*cursor)) {
            return false;
        }
    }
    return true;
}

/*
 * 保证一整段 TCP 数据全部发送出去，并用互斥锁串行化所有发送者。
 * 网络接收任务和其他业务任务都可以调用；不能在 ISR 中调用，因为可能阻塞。
 * 返回 false 表示连接已经不能继续发送，调用者应等待重连。
 */
static bool wifi_server_send_all(int socket_fd, const char *data, size_t length)
{
    if (s_socket_mutex == NULL ||
        xSemaphoreTake(s_socket_mutex, portMAX_DELAY) != pdTRUE) {
        return false;
    }

    size_t sent = 0U; // 记录当前已经成功发送的字节数
    while (sent < length) {
        // send() 可能只发送一部分数据，因此从 data + sent 继续发送剩余内容。
        const int result = send(socket_fd, data + sent, length - sent, 0);
        if (result <= 0) {
            xSemaphoreGive(s_socket_mutex);
            return false;
        }
        sent += (size_t)result;
    }

    xSemaphoreGive(s_socket_mutex);
    return true;
}

/*
 * 按 printf 格式生成一行回复，并自动在末尾添加 '\n' 后完整发送。
 * 普通状态下直接发送 OK/ERR；处理 FROM 请求时自动封装为 RESULT 帧。
 * 格式化结果超过缓冲区时会安全截断，不能在 ISR 中调用。
 */
static bool wifi_server_reply(int socket_fd, const char *format, ...)
{
    char payload[WIFI_SERVER_RX_BUFFER_SIZE];
    char response[WIFI_SERVER_RX_BUFFER_SIZE];
    va_list arguments;

    // va_start()/va_end() 用于读取 format 后面数量不固定的参数。
    va_start(arguments, format);
    // vsnprintf() 按 format 格式把可变参数写入 payload，并限制最大写入长度。
    const int payload_length = vsnprintf(
        payload, sizeof(payload), format, arguments);
    va_end(arguments);
    if (payload_length < 0) {
        return false;
    }

    if (s_result_context_active) {
        // 目标 ESP 执行完服务器转发的命令后，必须带上源 ID 和请求 ID 返回结果。
        const int prefix_length = snprintf(response, sizeof(response),
                                           "RESULT %s %s %s ",
                                           WIFI_DEVICE_ID,
                                           s_result_source,
                                           s_result_request_id);
        if (prefix_length < 0) {
            return false;
        }
        const size_t current_length = strnlen(response, sizeof(response));
        const size_t available = sizeof(response) - current_length - 1U;
        const size_t payload_size = strnlen(payload, sizeof(payload));
        const size_t copy_length = payload_size < available
                                       ? payload_size
                                       : available;
        memcpy(response + current_length, payload, copy_length);
        response[current_length + copy_length] = '\0';
    } else {
        snprintf(response, sizeof(response), "%s", payload);
    }

    // 为协议要求的换行符和 C 字符串结束符各预留一个字节。
    size_t response_length = strnlen(response, sizeof(response) - 2U);
    response[response_length++] = '\n';
    response[response_length] = '\0';
    return wifi_server_send_all(socket_fd, response, response_length);
}

/*
 * 删除可修改字符串开头和结尾的空白字符。
 * 用于已经完成 '\n' 分帧的命令，不应传入只读字符串常量。
 * 返回值可能指向原缓冲区中间，并通过写入 '\0' 截断尾部空白。
 */
static char *wifi_server_trim(char *text)
{
    // 跳过开头空白；强制转 unsigned char 可避免 isspace() 接收到负值。
    while (isspace((unsigned char)*text)) {
        text++;
    }

    char *end = text + strlen(text); // end 先指向原字符串结尾的 '\0'
    // end[-1] 等价于 *(end - 1)，表示 end 前面的最后一个有效字符。
    while (end > text && isspace((unsigned char)end[-1])) {
        end--;
    }
    *end = '\0';
    return text;
}

/*
 * 把上位机文本中的 "S1" 或 "S2" 转换为 ch446_bank_t 枚举值。
 * 仅用于协议解析；无效名称返回 false，不修改 CH446 硬件。
 */
static bool wifi_server_parse_bank(const char *text, ch446_bank_t *bank)
{
    // strcasecmp() 忽略字母大小写比较字符串；相等时返回 0。
    if (strcasecmp(text, "S1") == 0) {
        *bank = CH446_BANK_S1;
        return true;
    }
    if (strcasecmp(text, "S2") == 0) {
        *bank = CH446_BANK_S2;
        return true;
    }
    return false;
}

/* 把协议中的 Y0~Y4 转换为 CH446 测量总线；不接受其他 Y 编号。 */
static bool wifi_server_parse_bus(const char *text, ch446_bus_t *bus)
{
    if ((text[0] == 'Y' || text[0] == 'y') &&
        text[1] >= '0' && text[1] <= '4' && text[2] == '\0') {
        *bus = (ch446_bus_t)(text[1] - '0');
        return true;
    }
    return false;
}

/*
 * 把 "CONNECT S1 0 S2 0" 解析成两个 ch446_port_t 端口。
 * 这里只检查格式、端口组和 X 编号范围，不建立实际测量通路。
 * extra 字段用于拒绝命令末尾多出来的非空白参数。
 */
static bool wifi_server_parse_port_pair(
    const char *command,
    const char *expected_command,
    wifi_server_port_pair_t *pair)
{
    char command_name[10];
    char positive_bank_text[4];
    char negative_bank_text[4];
    char extra;
    unsigned int positive_x;
    unsigned int negative_x;

    // sscanf() 依次读取：命令名、正端 S 组、正端 X、负端 S 组、负端 X。
    // 最后的 %c 用来探测是否还有第六个多余字段，因此正确命令必须正好读到 5 项。
    const int field_count = sscanf(command, "%9s %3s %u %3s %u %c",
                                   command_name,
                                   positive_bank_text,
                                   &positive_x,
                                   negative_bank_text,
                                   &negative_x,
                                   &extra);
    if (field_count != 5 ||
        strcasecmp(command_name, expected_command) != 0 ||
        positive_x >= CH446_X_COUNT || negative_x >= CH446_X_COUNT) {
        return false;
    }

    ch446_bank_t positive_bank;
    ch446_bank_t negative_bank;
    if (!wifi_server_parse_bank(positive_bank_text, &positive_bank) ||
        !wifi_server_parse_bank(negative_bank_text, &negative_bank)) {
        return false;
    }

    // 使用 C 复合字面量把解析结果写入端口对结构体。
    pair->positive_port = (ch446_port_t){
        .bank = positive_bank,
        .x = (uint8_t)positive_x,
    };
    pair->negative_port = (ch446_port_t){
        .bank = negative_bank,
        .x = (uint8_t)negative_x,
    };
    return true;
}

/* 把 ch446_bank_t 枚举值转换为回复协议使用的 "S1" 或 "S2"。 */
static const char *wifi_server_bank_name(ch446_bank_t bank)
{
    // 三目运算符 condition ? value1 : value2：条件成立返回 S1，否则返回 S2。
    return bank == CH446_BANK_S1 ? "S1" : "S2";
}

static void wifi_server_format_status_hex(
    const uint8_t *bytes, size_t byte_count, char *text, size_t text_size)
{
    static const char digits[] = "0123456789ABCDEF";
    if (text_size == 0U) {
        return;
    }
    size_t index = 0U;
    for (size_t byte = 0U; byte < byte_count && index + 2U < text_size;
         byte++) {
        text[index++] = digits[(bytes[byte] >> 4U) & 0x0FU];
        text[index++] = digits[bytes[byte] & 0x0FU];
    }
    text[index] = '\0';
}

static void wifi_server_handle_status(int socket_fd)
{
    ch446_matrix_status_t status;
    const esp_err_t error = ch446_get_status(&status);
    if (error != ESP_OK) {
        wifi_server_reply(socket_fd, "ERR STATUS %s", esp_err_to_name(error));
        return;
    }
    char s1_hex[CH446_STATUS_BYTES_PER_CHIP * 2U + 1U];
    char s2_hex[CH446_STATUS_BYTES_PER_CHIP * 2U + 1U];
    wifi_server_format_status_hex(status.chip[CH446_CHIP_U1],
                                  CH446_STATUS_BYTES_PER_CHIP,
                                  s1_hex,
                                  sizeof(s1_hex));
    wifi_server_format_status_hex(status.chip[CH446_CHIP_U2],
                                  CH446_STATUS_BYTES_PER_CHIP,
                                  s2_hex,
                                  sizeof(s2_hex));
    wifi_server_reply(socket_fd, "OK STATUS S1 %s S2 %s", s1_hex, s2_hex);
}

/* Slaves have no low-resistance module; retain the legacy command with a clear error. */
static void wifi_server_handle_measure(int socket_fd)
{
    wifi_server_reply(socket_fd, "ERR MEASURE UNSUPPORTED_ON_SLAVE");
}

/*
 * 处理 CONNECT 命令，在两块丝印端口之间建立四线测量通路。
 * 只能用于合法的 S1/S2 X0~X23 端口；会先复位并重新配置 CH446 矩阵。
 * 成功后通路会保持闭合，直到 RESET 或后续选路/单节点命令改变它。
 */
static void wifi_server_handle_connect(int socket_fd, const char *command)
{
    wifi_server_port_pair_t pair;
    if (!wifi_server_parse_port_pair(command, "CONNECT", &pair)) {
        wifi_server_reply(socket_fd,
                          "ERR CONNECT usage=CONNECT S1 0 S2 0");
        return;
    }

    esp_err_t error = rs485_slave_debug_lock();
    if (error == ESP_OK) {
        error = ch446_connect_kelvin_pair(pair.positive_port, pair.negative_port);
        rs485_slave_debug_unlock();
    }
    if (error != ESP_OK) {
        wifi_server_reply(socket_fd, "ERR CONNECT %s", esp_err_to_name(error));
        return;
    }

    wifi_server_reply(socket_fd, "OK CONNECT %s %u %s %u",
                      wifi_server_bank_name(pair.positive_port.bank),
                      pair.positive_port.x,
                      wifi_server_bank_name(pair.negative_port.bank),
                      pair.negative_port.x);
}

/*
 * 处理 SWITCH：单独闭合或断开一个 X-Y 交叉点，并保留矩阵其他节点状态。
 * 本命令不会在操作前后复位矩阵，适合逐条命令搭建和排查原始通路。
 */
static void wifi_server_handle_switch(int socket_fd, const char *command)
{
    char command_name[10];
    char bank_text[4];
    char bus_text[3];
    char state_text[4];
    char extra;
    unsigned int x;

    const int field_count = sscanf(command, "%9s %3s %u %2s %3s %c",
                                   command_name,
                                   bank_text,
                                   &x,
                                   bus_text,
                                   state_text,
                                   &extra);
    ch446_bank_t bank;
    ch446_bus_t bus;
    bool closed;
    if (field_count != 5 ||
        strcasecmp(command_name, "SWITCH") != 0 ||
        x >= CH446_X_COUNT ||
        !wifi_server_parse_bank(bank_text, &bank) ||
        !wifi_server_parse_bus(bus_text, &bus)) {
        wifi_server_reply(socket_fd,
                          "ERR SWITCH usage=SWITCH S1 0 Y4 ON|OFF");
        return;
    }

    if (strcasecmp(state_text, "ON") == 0) {
        closed = true;
    } else if (strcasecmp(state_text, "OFF") == 0) {
        closed = false;
    } else {
        wifi_server_reply(socket_fd,
                          "ERR SWITCH usage=SWITCH S1 0 Y4 ON|OFF");
        return;
    }

    const ch446_port_t port = {
        .bank = bank,
        .x = (uint8_t)x,
    };
    esp_err_t error = rs485_slave_debug_lock();
    if (error == ESP_OK) {
        error = ch446_set_port_bus(port, bus, closed);
        rs485_slave_debug_unlock();
    }
    if (error != ESP_OK) {
        wifi_server_reply(socket_fd, "ERR SWITCH %s", esp_err_to_name(error));
        return;
    }

    wifi_server_reply(socket_fd, "OK SWITCH %s %u Y%u %s",
                      wifi_server_bank_name(bank),
                      x,
                      (unsigned int)bus,
                      closed ? "ON" : "OFF");
}

/*
 * 把一条已经完成 TCP 分帧的本地硬件命令分发给对应处理函数。
 * 仅处理 PING/MEASURE/RESET/CONNECT/SWITCH/HELP，不解析 SEND 或 FROM。
 * 命令处理可能访问 UART、GPIO、CH446，并通过 wifi_server_reply() 发送一次回复。
 */
static void wifi_server_handle_command(int socket_fd, char *raw_command)
{
    char *command = wifi_server_trim(raw_command);
    ESP_LOGI(TAG, "Command: %s", command);

    // *command 是字符串第一个字符；如果它就是 '\0'，说明裁剪后是一条空命令。
    if (*command == '\0') { // 空行没有可执行内容，直接忽略
        return;
    }

    // PING：通信检查；STATUS：读取软件状态位图；MEASURE：明确返回从机不支持；RESET：断开全部矩阵交叉点。
    // 带参数的选路和单节点命令只比较固定命令前缀，再由处理函数校验完整格式。
    if (strcasecmp(command, "PING") == 0) {
        wifi_server_reply(socket_fd, "OK PONG");
    } else if (strcasecmp(command, "STATUS") == 0) {
        wifi_server_handle_status(socket_fd);
    } else if (strcasecmp(command, "MEASURE") == 0) {
        wifi_server_handle_measure(socket_fd);
    } else if (strcasecmp(command, "RESET") == 0) {
        esp_err_t error = rs485_slave_debug_lock();
        if (error == ESP_OK) {
            error = ch446_reset_all();
            rs485_slave_debug_unlock();
        }
        if (error == ESP_OK) {
            wifi_server_reply(socket_fd, "OK RESET");
        } else {
            wifi_server_reply(socket_fd, "ERR RESET %s", esp_err_to_name(error));
        }
    } else if (strncasecmp(command, "CONNECT ", 8U) == 0) {
        wifi_server_handle_connect(socket_fd, command);
    } else if (strncasecmp(command, "SWITCH ", 7U) == 0) {
        wifi_server_handle_switch(socket_fd, command);
    } else if (strcasecmp(command, "HELP") == 0) {
        wifi_server_reply(socket_fd,
                           "OK HELP PING|STATUS|RESET|CONNECT S1 0 S2 0|SWITCH S1 0 Y4 ON|HELP");
    } else {
        wifi_server_reply(socket_fd, "ERR UNKNOWN_COMMAND");
    }
}

/*
 * 解析电脑路由服务器发来的 "FROM 源ID 请求ID 载荷" 帧。
 * payload 保留后续所有空格，以便继续解析 CONNECT/SWITCH 等本地命令。
 * 本函数只拆分字段，不执行命令，也不发送网络数据。
 */
static bool wifi_server_parse_forward(
    char *command,
    char *source,
    char *request_id,
    char *payload)
{
    return sscanf(command, "FROM %31s %31s %223[^\n]",
                  source, request_id, payload) == 3;
}

/*
 * 执行一条 FROM 转发帧，并把本地命令回复自动封装成 RESULT。
 * 仅应由电脑路由服务器的 TCP 接收路径调用，不应把普通本地命令传入这里。
 * 执行期间会临时设置全局回复上下文；当前设计要求命令按单线程顺序处理。
 */
static void wifi_server_handle_forward(int socket_fd, char *command)
{
    char source[WIFI_SERVER_ID_SIZE];
    char request_id[WIFI_SERVER_ID_SIZE];
    char payload[WIFI_SERVER_RX_BUFFER_SIZE];
    if (!wifi_server_parse_forward(command, source, request_id, payload)) {
        wifi_server_reply(socket_fd, "ERR INVALID_FORWARD");
        return;
    }

    // 保存原请求者和请求 ID，使 wifi_server_reply() 能把结果送回正确设备。
    snprintf(s_result_source, sizeof(s_result_source), "%s", source);
    snprintf(s_result_request_id, sizeof(s_result_request_id), "%s", request_id);
    s_result_context_active = true;
    wifi_server_handle_command(socket_fd, payload);
    s_result_context_active = false; // 本条命令完成，后续普通回复不再封装为 RESULT
}

/*
 * 持续接收电脑服务器的数据，并以 '\n' 为边界拼接完整协议帧。
 * TCP 是字节流，一条命令可能跨多次 recv()，一次 recv() 也可能含多条命令。
 * 本函数只处理当前连接；返回表示连接关闭、网络错误或 Wi-Fi 已断开。
 */
static void wifi_server_receive_loop(int socket_fd)
{
    char receive_buffer[WIFI_SERVER_RX_BUFFER_SIZE]; // 保存本次 recv() 收到的一批字节
    char command_buffer[WIFI_SERVER_RX_BUFFER_SIZE]; // 跨多次 recv() 拼接当前协议行
    size_t command_length = 0U; // command_buffer 已经保存的字符数量
    bool overflow = false;      // 当前行超过缓冲区后，丢弃到下一个 '\n'
    TickType_t last_heartbeat_tick = xTaskGetTickCount();
    const struct timeval receive_timeout = {
        .tv_sec = 1,
        .tv_usec = 0,
    };
    setsockopt(socket_fd, SOL_SOCKET, SO_RCVTIMEO,
               &receive_timeout, sizeof(receive_timeout));

    while (true) {
        const TickType_t now = xTaskGetTickCount();
        if ((now - last_heartbeat_tick) >=
            pdMS_TO_TICKS(WIFI_SERVER_HEARTBEAT_INTERVAL_MS)) {
            // 心跳和业务发送共用发送锁，避免 PING 与 RESULT/SEND 字节交叉。
            static const char heartbeat[] = "PING\n";
            if (!wifi_server_send_all(socket_fd, heartbeat,
                                      sizeof(heartbeat) - 1U)) {
                ESP_LOGW(TAG, "Heartbeat send failed; reconnecting");
                return;
            }
            last_heartbeat_tick = now;
        }

        const int received = recv(socket_fd, receive_buffer,
                                  sizeof(receive_buffer), 0);
        // 接收超时不是断线：Wi-Fi 仍在线时继续等待，否则让外层任务进入重连。
        if (received < 0 &&
            (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            if ((xEventGroupGetBits(s_wifi_event_group) &
                 WIFI_SERVER_WIFI_CONNECTED_BIT) != 0U) {
                continue;
            }
            return;
        }
        if (received <= 0) {
            return;
        }

        for (int index = 0; index < received; index++) {
            const char byte = receive_buffer[index];
            if (byte == '\r') { // 忽略 CR，同时兼容 "\n" 和 "\r\n" 行尾
                continue;
            }
            if (byte == '\n') {
                // 收到换行说明一条完整协议帧结束，可以补 '\0' 作为 C 字符串。
                if (overflow) {
                    ESP_LOGW(TAG, "Server line too long");
                } else {
                    command_buffer[command_length] = '\0';
                    char *command = wifi_server_trim(command_buffer);
                    if (strncasecmp(command, "FROM ", 5U) == 0) {
                        wifi_server_handle_forward(socket_fd, command);
                    } else if (strcasecmp(command, "OK PONG") == 0) {
                        // 周期心跳应答不占用常规串口日志，调试时仍可按需查看。
                        ESP_LOGD(TAG, "Heartbeat acknowledged");
                    } else {
                        // OK FORWARDED、ERR、RESULT 等非命令帧当前先记录到串口日志。
                        ESP_LOGI(TAG, "Server: %s", command);
                    }
                }
                command_length = 0U; // 当前行处理完成，下一字节开始拼接新命令
                overflow = false;    // 新命令重新允许写入 command_buffer
                continue;            // 只结束本次 for 迭代，不退出 while 或函数
            }

            if (!overflow) {
                if (command_length + 1U < sizeof(command_buffer)) {
                    command_buffer[command_length++] = byte;
                } else {
                    overflow = true; // 停止写缓冲区，但继续找本行末尾的 '\n'
                }
            }
        }
    }
}

/*
 * 从本机向指定目标设备发送一条路由请求。
 * 业务任务可在 wifi_server_start() 成功且 TCP 已连接后调用；不能在 ISR 中调用。
 * 发送格式为 "SEND 己方ID 目标ID 请求ID 载荷"，函数只表示请求写入 TCP，
 * 目标是否在线和是否执行成功要分别等待 OK FORWARDED/ERR/RESULT 帧确认。
 */
esp_err_t wifi_server_send_request(const char *target_id,
                                   const char *request_id,
                                   const char *payload)
{
    if (!wifi_server_valid_token(target_id) ||
        !wifi_server_valid_token(request_id) || payload == NULL ||
        *payload == '\0' ||
        strchr(payload, '\n') != NULL || strchr(payload, '\r') != NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (s_socket_mutex == NULL ||
        xSemaphoreTake(s_socket_mutex, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_INVALID_STATE;
    }
    // 在互斥锁保护下读取当前连接句柄，避免与断线清理同时修改该变量。
    const int socket_fd = s_connection_socket;
    xSemaphoreGive(s_socket_mutex);
    if (socket_fd < 0) {
        return ESP_ERR_INVALID_STATE;
    }

    char request[WIFI_SERVER_RX_BUFFER_SIZE]; // 保存最终带源/目标/请求 ID 的协议帧
    const int length = snprintf(request, sizeof(request),
                                "SEND %s %s %s %s\n",
                                WIFI_DEVICE_ID, target_id, request_id, payload);
    if (length < 0 || (size_t)length >= sizeof(request)) {
        return ESP_ERR_INVALID_SIZE;
    }
    return wifi_server_send_all(socket_fd, request, (size_t)length)
               ? ESP_OK
               : ESP_FAIL;
}

/*
 * 关闭当前电脑服务器连接，并把全局 socket 状态恢复为“未连接”。
 * 只由 TCP 客户端任务在发送失败或接收循环结束后调用。
 * shutdown()/close() 与其他发送者共用互斥锁，避免关闭正在发送的 socket。
 */
static void wifi_server_close_connection(int socket_fd)
{
    if (s_socket_mutex != NULL) {
        xSemaphoreTake(s_socket_mutex, portMAX_DELAY);
    }
    if (s_connection_socket == socket_fd) {
        s_connection_socket = -1;
    }
    shutdown(socket_fd, SHUT_RDWR);
    close(socket_fd);
    if (s_socket_mutex != NULL) {
        xSemaphoreGive(s_socket_mutex);
    }
}

/*
 * FreeRTOS TCP 客户端任务：等待 STA 获得 IP、连接电脑路由服务器并维持会话。
 * 连接成功后先发送 HELLO ROLE 注册设备 ID，然后进入按行接收循环。
 * socket 创建、connect() 或会话失败时自动延时重试；该任务设计为永久运行。
 */
static void wifi_server_task(void *argument)
{
    (void)argument; // xTaskCreate() 没有传入业务参数，显式标记为未使用

    while (true) {
        // TCP connect() 前必须等待 DHCP 完成并取得 IP，避免无网络时反复创建 socket。
        xEventGroupWaitBits(s_wifi_event_group,
                            WIFI_SERVER_WIFI_CONNECTED_BIT,
                            pdFALSE,
                            pdTRUE,
                            portMAX_DELAY);

        // GOT_IP 回调先保存网关再设置事件位，因此唤醒后可复制一个稳定的连接目标。
        const esp_ip4_addr_t server_address_snapshot = s_server_address;

        // AF_INET 表示 IPv4，SOCK_STREAM 表示 TCP 字节流。
        const int socket_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
        if (socket_fd < 0) {
            ESP_LOGE(TAG, "Unable to create socket: errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        // 电脑开启热点时会作为 DHCP 默认网关，ESP 无需写死其具体 IPv4 地址。
        const struct sockaddr_in server_address = {
            .sin_family = AF_INET,
            .sin_port = htons(WIFI_SERVER_TCP_PORT),
            .sin_addr.s_addr = server_address_snapshot.addr,
        };
        // ESP32 在新架构中是 TCP 客户端，因此使用 connect()，不再 bind/listen/accept。
        if (connect(socket_fd,
                    (const struct sockaddr *)&server_address,
                    sizeof(server_address)) != 0) {
            ESP_LOGW(TAG, "Unable to connect server " IPSTR ":%d errno=%d",
                     IP2STR(&server_address_snapshot),
                     WIFI_SERVER_TCP_PORT, errno);
            close(socket_fd);
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }

        s_connection_socket = socket_fd; // 对外发送接口从此刻起可以使用该连接
        // 每次 TCP 重连都重新注册；服务器依靠该 ID 建立“设备ID -> socket”映射。
        char hello[WIFI_SERVER_ID_SIZE * 2U];
        const int hello_length = snprintf(hello,
                                          sizeof(hello),
                                          "HELLO %s %s\n",
                                          WIFI_NODE_ROLE,
                                          WIFI_DEVICE_ID);
        if (hello_length < 0 || (size_t)hello_length >= sizeof(hello)) {
            wifi_server_close_connection(socket_fd);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        if (!wifi_server_send_all(socket_fd, hello, (size_t)hello_length)) {
            wifi_server_close_connection(socket_fd);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        ESP_LOGI(TAG, "Connected to server " IPSTR ":%d as %s",
                 IP2STR(&server_address_snapshot),
                 WIFI_SERVER_TCP_PORT, WIFI_DEVICE_ID);

        wifi_server_receive_loop(socket_fd);
        wifi_server_close_connection(socket_fd);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

/*
 * ESP-IDF Wi-Fi/IP 事件回调：启动连接、处理掉线重连并发布“已取得 IP”状态。
 * 该回调运行在系统事件任务中，只做轻量状态操作，不执行阻塞的 TCP 连接。
 */
static void wifi_server_event_handler(void *argument,
                                      esp_event_base_t event_base,
                                      int32_t event_id,
                                      void *event_data)
{
    (void)argument;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        // Wi-Fi 驱动启动后，主动尝试关联配置好的热点。
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT &&
               event_id == WIFI_EVENT_STA_DISCONNECTED) {
        // 清除联网标志，TCP 任务会退出当前会话并等待下一次 GOT_IP。
        xEventGroupClearBits(s_wifi_event_group,
                             WIFI_SERVER_WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "Wi-Fi disconnected; retrying");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT &&
               event_id == IP_EVENT_STA_GOT_IP) {
        // DHCP 已分配地址和默认网关；电脑热点场景中默认网关就是 GUI 所在电脑。
        const ip_event_got_ip_t *event = event_data;
        if (event->ip_info.gw.addr == 0U) {
            xEventGroupClearBits(s_wifi_event_group,
                                 WIFI_SERVER_WIFI_CONNECTED_BIT);
            ESP_LOGE(TAG, "DHCP provided no default gateway; server address unavailable");
            return;
        }
        s_server_address = event->ip_info.gw;
        xEventGroupSetBits(s_wifi_event_group,
                           WIFI_SERVER_WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "STA got IP: " IPSTR ", server gateway: " IPSTR,
                 IP2STR(&event->ip_info.ip),
                 IP2STR(&event->ip_info.gw));
    }
}

/*
 * 初始化 NVS、网络接口、事件循环和 Wi-Fi STA，并创建永久 TCP 客户端任务。
 * 只应在 app_main() 的系统初始化阶段调用一次；重复调用会直接返回 ESP_OK。
 * 本函数会创建事件组、互斥锁和 FreeRTOS 任务，并启动 Wi-Fi 驱动。
 */
esp_err_t wifi_server_start(void)
{
    if (s_started) { // 防止重复创建默认网络接口、事件回调和客户端任务
        return ESP_OK;
    }

    // Wi-Fi 驱动依赖 NVS 保存部分配置和射频校准数据。
    esp_err_t error = nvs_flash_init();
    if (error == ESP_ERR_NVS_NO_FREE_PAGES ||
        error == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        // 仅在 NVS 空间耗尽或版本不兼容时擦除默认 NVS 分区，再重新初始化。
        // 这不会擦除应用固件，但会清除默认 NVS 中保存的其他键值数据。
        ESP_ERROR_CHECK(nvs_flash_erase());
        error = nvs_flash_init();
    }
    if (error != ESP_OK) {
        return error;
    }

    error = esp_netif_init(); // 初始化 ESP-IDF TCP/IP 网络接口层
    if (error != ESP_OK) {
        return error;
    }
    error = esp_event_loop_create_default(); // 创建 Wi-Fi/IP 系统事件循环
    if (error != ESP_OK) {
        return error;
    }
    if (esp_netif_create_default_wifi_sta() == NULL) { // 创建默认 STA 和 DHCP 客户端
        return ESP_FAIL;
    }

    s_wifi_event_group = xEventGroupCreate(); // 在 IP 回调和 TCP 任务间传递联网状态
    s_socket_mutex = xSemaphoreCreateMutex(); // 保护 socket 发送和关闭操作
    if (s_wifi_event_group == NULL || s_socket_mutex == NULL) {
        return ESP_ERR_NO_MEM;
    }

    // 使用 ESP-IDF 推荐默认值初始化底层 Wi-Fi 驱动资源。
    const wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    error = esp_wifi_init(&init_config);
    if (error != ESP_OK) {
        return error;
    }
    // 监听全部 Wi-Fi 事件，用于 STA_START 和 STA_DISCONNECTED。
    error = esp_event_handler_register(WIFI_EVENT,
                                       ESP_EVENT_ANY_ID,
                                       wifi_server_event_handler,
                                       NULL);
    if (error != ESP_OK) {
        return error;
    }
    // 只监听 STA 获得 IP 事件，成功后唤醒 TCP 客户端任务。
    error = esp_event_handler_register(IP_EVENT,
                                       IP_EVENT_STA_GOT_IP,
                                       wifi_server_event_handler,
                                       NULL);
    if (error != ESP_OK) {
        return error;
    }

    // 配置要连接的电脑热点或局域网 AP；SSID 和密码在 wifi_server.h 中修改。
    wifi_config_t station_config = {
        .sta = {
            .ssid = WIFI_STA_SSID,
            .password = WIFI_STA_PASSWORD,
            .scan_method = WIFI_FAST_SCAN,
        },
    };

    error = esp_wifi_set_mode(WIFI_MODE_STA); // ESP32 作为无线终端，不再建立 SoftAP
    if (error != ESP_OK) {
        return error;
    }
    error = esp_wifi_set_config(WIFI_IF_STA, &station_config); // 把热点参数交给 STA 接口
    if (error != ESP_OK) {
        return error;
    }
    error = esp_wifi_start(); // 启动后会产生 WIFI_EVENT_STA_START 并触发连接
    if (error != ESP_OK) {
        return error;
    }

    // 创建长期运行的 TCP 客户端任务；app_main() 返回后该任务仍会继续运行。
    if (xTaskCreate(wifi_server_task,
                    "wifi_client",
                    WIFI_SERVER_TASK_STACK_SIZE,
                    NULL,
                    WIFI_SERVER_TASK_PRIORITY,
                    NULL) != pdPASS) {
        esp_wifi_stop(); // 任务创建失败时停止已经启动的 Wi-Fi，避免留下无消费者的网络
        return ESP_ERR_NO_MEM;
    }

    s_started = true;
    ESP_LOGI(TAG, "STA ready: SSID=%s device_id=%s server=DHCP-gateway:%d",
             WIFI_STA_SSID, WIFI_DEVICE_ID, WIFI_SERVER_TCP_PORT);
    return ESP_OK;
}
