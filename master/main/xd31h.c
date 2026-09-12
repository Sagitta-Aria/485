#include "xd31h.h"

#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "board_config.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define XD31H_SLAVE_ADDRESS       0x01  //模块从机地址
#define XD31H_READ_FUNCTION       0x03   //读取寄存器功能码
#define XD31H_REGISTER_ADDRESS    0x0000  //寄存器读取地址 占两个字节
#define XD31H_REGISTER_COUNT      0x0002   //寄存器读取数量  （读取四字节数据：一个寄存器16位，2字节）
#define XD31H_REQUEST_LENGTH      8   //请求帧长度
#define XD31H_RESPONSE_LENGTH     XD31H_MAX_RESPONSE_LENGTH //响应帧长度
#define XD31H_EXCEPTION_LENGTH    5   //异常帧长度
#define XD31H_RX_BUFFER_SIZE      256   //接收缓冲区大小

static const char *TAG = "xd31h";  //日志标签
static bool s_initialized;    //模块初始化标志

/* Translate standard Modbus exception bytes while preserving unknown values. */
static const char *xd31h_modbus_exception_name(uint8_t exception_code)
{
    switch (exception_code) {
    case 0x01:
        return "ILLEGAL_FUNCTION";
    case 0x02:
        return "ILLEGAL_DATA_ADDRESS";
    case 0x03:
        return "ILLEGAL_DATA_VALUE";
    case 0x04:
        return "SLAVE_DEVICE_FAILURE";
    default:
        return "UNKNOWN";
    }
}

/* Preserve every received byte so network diagnostics can reproduce the frame. */
static void xd31h_store_response(xd31h_measurement_t *measurement,
                                 const uint8_t *frame,
                                 size_t frame_length)
{
    const size_t copy_length = frame_length < sizeof(measurement->response)
                                   ? frame_length
                                   : sizeof(measurement->response);
    memcpy(measurement->response, frame, copy_length);
    measurement->response_length = copy_length;
}

// CRC16计算函数
static uint16_t xd31h_modbus_crc16(const uint8_t *data, size_t length)   //计算CRC16校验码
{
    uint16_t crc = 0xFFFF;

    for (size_t byte_index = 0; byte_index < length; byte_index++) {
        crc ^= data[byte_index];   //^= 是异或赋值   相当于crc = crc ^ data[byte_index];  异或：不同则置一

        for (uint8_t bit_index = 0; bit_index < 8; bit_index++) {
            if ((crc & 0x0001U) != 0U) {
                crc = (crc >> 1U) ^ 0xA001U;   //往右移一位 然后与 Modbus CRC16规定的多项式 0xA001 异或
            } else {
                crc >>= 1U;
            }
        }
    }

    return crc;
}

// 接收帧函数
static esp_err_t xd31h_receive_frame(
    uint8_t *frame,  //接收缓冲区
    size_t capacity,  //接收缓冲区容量
    size_t *frame_length,  //接收帧长度
    uint32_t timeout_ms //超时时间（毫秒）
)
{
     *frame_length = 0U;  //初始化接收帧长度为0，表示尚未接收到任何数据
    const TickType_t start_tick = xTaskGetTickCount();   //CONFIG_FREERTOS_HZ=100  一个tick=10ms
    TickType_t timeout_ticks = pdMS_TO_TICKS(timeout_ms);  //将毫秒转换为FreeRTOS的滴答数
    size_t received = 0;
    size_t expected_length = 0;  //期望接收的帧长度

    if (timeout_ticks == 0) {
        timeout_ticks = 1;  //确保至少等待一个滴答，以避免立即超时
    }

    while (received < capacity) {
        const TickType_t elapsed_ticks = xTaskGetTickCount() - start_tick;  //计算已经经过的滴答数
        if (elapsed_ticks >= timeout_ticks) {
            break;
        }
        
        const int read_length = uart_read_bytes(   //从UART端口读取数据
            BOARD_XD31H_UART_PORT,  //XD31H模块使用的UART端口
            frame + received,   //接收缓冲区的当前写入位置
            capacity - received,  //剩余可用空间
            timeout_ticks - elapsed_ticks   //剩余等待时间
        );

        if (read_length < 0) {
            return ESP_FAIL;
        }
        if (read_length == 0) {
            break;
        }

        received += (size_t)read_length;
        *frame_length = received;//更新接收帧长度，表示已经接收到的数据长度

        //根据Modbus RTU协议，确定期望接收的帧长度   &&为且
        if (received >= 2 && (frame[1] & 0x80U) != 0U) {    //received >= 2：至少已经收到设备地址和功能码，确保读取 frame[1] 不会越界
            //frame[1] & 0x80U：检查功能码的最高位是否为 1
            expected_length = XD31H_EXCEPTION_LENGTH;  //设置为异常帧长度
        } else if (received >= 3) {  //至少已经收到设备地址、功能码和字节计数，确保读取 frame[2] 不会越界
            expected_length = (size_t)frame[2] + 5U;  //设置为响应帧长度：字节计数 + 5（设备地址、功能码、字节计数和 CRC16 校验码）
            if (expected_length > capacity) {
                return ESP_ERR_INVALID_SIZE;  //如果期望长度超过缓冲区容量，返回错误
            }
        }

        //如果期望长度不为零且已接收的数据长度大于或等于期望长度，则认为接收完成
        if (expected_length != 0 && received >= expected_length) {
            *frame_length = expected_length;
            return ESP_OK;
        }
    }

    *frame_length = received; //将实际接收的字节数存储在 frame_length 中
    return ESP_ERR_TIMEOUT;  
}

// 解析响应函数
static esp_err_t xd31h_parse_response   //esp_err_t 这个变量或返回值保存的是 ESP-IDF 错误码
(const uint8_t *frame, //接收到的帧数据
    size_t frame_length, //接收到的帧长度
    xd31h_measurement_t *measurement //指向存储测量结果的结构体的指针
)
{
    if (frame_length < XD31H_EXCEPTION_LENGTH) { //如果接收到的帧长度小于异常帧长度，说明数据不完整，返回错误
        measurement->diagnostic = XD31H_DIAGNOSTIC_FRAME_TOO_SHORT;
        return ESP_ERR_INVALID_SIZE;
    }

    const uint16_t received_crc = (uint16_t)frame[frame_length - 2] |
                                  ((uint16_t)frame[frame_length - 1] << 8U); //提取接收到的 CRC16 校验码，frame[frame_length - 2] 是低字节，frame[frame_length - 1] 是高字节
    const uint16_t calculated_crc = xd31h_modbus_crc16(frame, frame_length - 2);  //计算接收到的帧数据的 CRC16 校验码，frame_length - 2 表示不包括最后两个字节的 CRC16 校验码
    measurement->received_crc = received_crc;
    measurement->calculated_crc = calculated_crc;

    if (received_crc != calculated_crc) {//如果接收到的 CRC16 校验码与计算得到的 CRC16 校验码不匹配，说明数据可能在传输过程中被篡改或损坏，返回错误
        measurement->diagnostic = XD31H_DIAGNOSTIC_CRC_MISMATCH;
        ESP_LOGE(TAG, "CRC mismatch: received=0x%04X calculated=0x%04X",
                 received_crc, calculated_crc);
        return ESP_ERR_INVALID_CRC;
    }

    if (frame[0] != XD31H_SLAVE_ADDRESS) {  //如果接收到的帧的设备地址与预期的从机地址不匹配，说明响应不是来自预期的设备，返回错误
        measurement->diagnostic = XD31H_DIAGNOSTIC_SLAVE_ADDRESS;
        ESP_LOGE(TAG, "Unexpected slave address: 0x%02X", frame[0]);
        return ESP_ERR_INVALID_RESPONSE;
    }

    if (frame[1] == (XD31H_READ_FUNCTION | 0x80U)) {  //如果接收到的帧的功能码是读取功能码加上 0x80，说明设备返回了一个异常响应，表示请求无法被处理
        measurement->diagnostic = XD31H_DIAGNOSTIC_MODBUS_EXCEPTION;
        ESP_LOGE(TAG, "Modbus exception code: 0x%02X", frame[2]);
        return ESP_ERR_INVALID_RESPONSE;
    }
    //Modbus 协议规定的异常响应格式 如果从机无法执行命令，就把原功能码的最高位 bit7 设置为 1

    // || 是逻辑或，只要任何一个条件成立，就认为响应不正确
    if (frame_length != XD31H_RESPONSE_LENGTH ||
        frame[1] != XD31H_READ_FUNCTION || frame[2] != 4U) { //如果接收到的帧长度不等于预期的响应帧长度，或者功能码不等于读取功能码，或者字节计数不等于 4，说明响应格式不正确，返回错误
        measurement->diagnostic = XD31H_DIAGNOSTIC_RESPONSE_FORMAT;
        ESP_LOGE(TAG, "Unexpected response format: length=%zu function=0x%02X byte_count=%u",
                 frame_length, frame[1], frame[2]); 
        return ESP_ERR_INVALID_RESPONSE;
    }

    measurement->status = frame[3];  //测量状态
    measurement->range = frame[4];  //测量范围，即精细度，单位
    measurement->raw_value = ((uint16_t)frame[5] << 8U) | frame[6];  //原始测量值（高字节在前，低字节在后）
    measurement->valid = (measurement->status == 0U);  //如果测量状态为 0，表示测量有效，否则表示测量无效
 
    if (!measurement->valid) {  //如果测量无效，直接返回 ESP_OK，表示解析成功，但测量结果无效
        measurement->diagnostic = measurement->status == 1U
                                      ? XD31H_DIAGNOSTIC_STATUS_OL
                                      : XD31H_DIAGNOSTIC_STATUS_UNKNOWN;
        return ESP_OK;  
    }

    switch (measurement->range) {
    case 0:
        measurement->resistance_ohm = measurement->raw_value / 10.0f;
        break;
    case 1:
        measurement->resistance_ohm = measurement->raw_value / 100.0f;
        break;
    case 2:
        measurement->resistance_ohm = measurement->raw_value / 1000.0f;
        break;
    default:
        measurement->valid = false;  //如果测量范围不在预期的范围内，标记测量无效
        measurement->diagnostic = XD31H_DIAGNOSTIC_INVALID_RANGE;
        return ESP_ERR_INVALID_RESPONSE;  //返回无效响应错误
    }

    return ESP_OK;  
}

// 初始化函数
esp_err_t xd31h_init(void)
{
    if (s_initialized) {  //如果已经初始化过，直接返回 ESP_OK，避免重复初始化
        return ESP_OK;
    }

    const uart_config_t uart_config = {
        .baud_rate = BOARD_XD31H_UART_BAUD_RATE,  //设置波特率为预定义的值
        .data_bits = UART_DATA_8_BITS,    //设置数据位为 8 位
        .parity = UART_PARITY_DISABLE,  //设置无奇偶校验
        .stop_bits = UART_STOP_BITS_1,  //设置停止位为 1 位
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,  //设置无硬件流控
        .source_clk = UART_SCLK_DEFAULT,  //设置时钟源为默认值
    };

    esp_err_t error = uart_param_config(BOARD_XD31H_UART_PORT, &uart_config);   //error 保存的是 uart_param_config() 的返回值
    if (error != ESP_OK) {  //error != ESP_OK  → 操作失败，立即退出当前函数
        return error;
    }//配置UART参数，包括波特率、数据位、奇偶校验、停止位、流控和时钟源等

    error = uart_set_pin(BOARD_XD31H_UART_PORT,
                         BOARD_XD31H_UART_TX_GPIO,
                         BOARD_XD31H_UART_RX_GPIO,
                         UART_PIN_NO_CHANGE,
                         UART_PIN_NO_CHANGE);
    if (error != ESP_OK) {  //设置UART引脚，包括TX和RX引脚，其他引脚保持不变
        return error;
    }

    error = uart_driver_install(BOARD_XD31H_UART_PORT,  //安装UART驱动程序，配置接收缓冲区大小为 XD31H_RX_BUFFER_SIZE，发送缓冲区大小为 0，使用默认事件队列
                                XD31H_RX_BUFFER_SIZE,
                                0,
                                0,
                                NULL,
                                0);
    if (error != ESP_OK) {
        return error;
    }

    s_initialized = true;  //标记模块已初始化
    ESP_LOGI(TAG, "UART%d initialized: TX=GPIO%d RX=GPIO%d, %d 8N1",  
             BOARD_XD31H_UART_PORT,
             BOARD_XD31H_UART_TX_GPIO,
             BOARD_XD31H_UART_RX_GPIO,
             BOARD_XD31H_UART_BAUD_RATE);   //打印日志

    return ESP_OK;
}


// 读取测量值函数
esp_err_t xd31h_read_measurement(
    xd31h_measurement_t *measurement,  //指向存储测量结果的结构体的指针
     uint32_t timeout_ms  //超时时间（毫秒）
    )  
{
    if (measurement == NULL) {
        return ESP_ERR_INVALID_ARG;  //如果传入的测量结构体指针为 NULL，返回无效参数错误
    }

    //memset() 是 C 标准库中的按字节填充内存函数  memset(目标地址, 填充值, 字节数量);
    memset(measurement, 0, sizeof(*measurement));  //将测量结构体清零，确保所有字段初始化为默认值
    if (timeout_ms == 0U) {
        measurement->diagnostic = XD31H_DIAGNOSTIC_INVALID_ARGUMENT;
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_initialized) {
        measurement->diagnostic = XD31H_DIAGNOSTIC_NOT_INITIALIZED;
        return ESP_ERR_INVALID_STATE;  //如果模块未初始化，返回无效状态错误
    }

    //创建一个用于存放 Modbus 请求帧的字节数组
    uint8_t request[XD31H_REQUEST_LENGTH] = {
        XD31H_SLAVE_ADDRESS,  //模块从机地址
        XD31H_READ_FUNCTION,  //读取寄存器功能码
        (uint8_t)(XD31H_REGISTER_ADDRESS >> 8U),  //寄存器地址的高字节
        (uint8_t)XD31H_REGISTER_ADDRESS,   //寄存器地址的低字节
        (uint8_t)(XD31H_REGISTER_COUNT >> 8U),  //寄存器数量的高字节
        (uint8_t)XD31H_REGISTER_COUNT,  //寄存器数量的低字节
        0,  //CRC16 校验码的低字节，稍后计算
        0,  //CRC16 校验码的高字节，稍后计算
    };

    const uint16_t request_crc = xd31h_modbus_crc16(request, XD31H_REQUEST_LENGTH - 2);//计算请求帧的 CRC16 校验码，排除最后两个字节（CRC16 字节）进行计算
    //将 16 位数的CRC16 强制转换为 uint8_t 时，会直接丢弃高 8 位，只保留最低 8 位
    request[6] = (uint8_t)request_crc;  //将计算得到的 CRC16 校验码的低字节存储在请求帧的第 6 个字节
    request[7] = (uint8_t)(request_crc >> 8U);  //将计算得到的 CRC16 校验码的高字节存储在请求帧的第 7 个字节

    esp_err_t error = uart_flush_input(BOARD_XD31H_UART_PORT);  //清空 UART 接收缓冲区，确保接收缓冲区中没有残留数据
    if (error != ESP_OK) {
        measurement->diagnostic = XD31H_DIAGNOSTIC_UART_FLUSH;
        ESP_LOGE(TAG, "Failed to flush UART input: %s", esp_err_to_name(error));
        return error;
    }

    const int written = uart_write_bytes(BOARD_XD31H_UART_PORT,  //将请求帧写入 UART 发送缓冲区
                                         request,   //请求帧数据
                                         XD31H_REQUEST_LENGTH);  //请求帧长度
    if (written != XD31H_REQUEST_LENGTH) {  //如果写入的字节数不等于请求帧长度，说明写入失败，返回错误
        measurement->diagnostic = XD31H_DIAGNOSTIC_UART_WRITE;
        measurement->diagnostic_value = written;
        ESP_LOGE(TAG, "Failed to write request frame: written=%d expected=%d", written, XD31H_REQUEST_LENGTH);
        return ESP_FAIL;
    }

    error = uart_wait_tx_done(BOARD_XD31H_UART_PORT, pdMS_TO_TICKS(100));//等待 UART 发送完成，超时时间为 100 毫秒
    if (error != ESP_OK) {  //如果等待发送完成失败，打印错误日志并返回错误
        measurement->diagnostic = XD31H_DIAGNOSTIC_UART_TRANSMIT;
        ESP_LOGE(TAG, "UART transmit failed: %s", esp_err_to_name(error));  //esp_err_to_name() 函数将错误码转换为可读的字符串
        return error;
    }

    uint8_t response[XD31H_RESPONSE_LENGTH] = {0};  //创建一个用于存放 Modbus 响应帧的字节数组，并初始化为 0
    size_t response_length = 0;  //用于存储实际接收到的响应帧长度
    error = xd31h_receive_frame(response,  //调用接收帧函数，从 UART 接收缓冲区读取响应帧数据
                                sizeof(response),  //响应缓冲区的大小
                                &response_length,  //实际接收到的响应帧长度
                                timeout_ms);  //超时时间（毫秒）
    xd31h_store_response(measurement, response, response_length);
    if (error != ESP_OK) {
        if (error == ESP_ERR_TIMEOUT) {
            measurement->diagnostic = XD31H_DIAGNOSTIC_TIMEOUT;
        } else if (error == ESP_ERR_INVALID_SIZE) {
            measurement->diagnostic = XD31H_DIAGNOSTIC_RESPONSE_TOO_LARGE;
            measurement->diagnostic_value = response_length >= 3U
                                                ? (int32_t)response[2] + 5
                                                : 0;
        } else {
            measurement->diagnostic = XD31H_DIAGNOSTIC_UART_RECEIVE;
        }
        if (response_length > 0) {  //如果接收到了一些数据，但仍然发生错误，打印接收到的响应帧的十六进制转储，以便调试
            ESP_LOG_BUFFER_HEXDUMP(TAG, response, response_length, ESP_LOG_WARN);
        }
        return error;//如果接收帧失败，返回错误
    }

    error = xd31h_parse_response(response, response_length, measurement); //调用解析响应函数，解析接收到的响应帧数据，并将测量结果存储在 measurement
    if (error != ESP_OK) {  //如果解析响应失败，打印接收到的响应帧的十六进制转储，以便调试
        ESP_LOG_BUFFER_HEXDUMP(TAG, response, response_length, ESP_LOG_ERROR);
    }

    return error;  
}

/* Convert the captured binary response into compact hexadecimal ASCII. */
static void xd31h_format_frame(const xd31h_measurement_t *measurement,
                               char *buffer,
                               size_t buffer_size)
{
    if (buffer_size == 0U) {
        return;
    }
    if (measurement->response_length == 0U) {
        snprintf(buffer, buffer_size, "-");
        return;
    }

    size_t offset = 0U;
    for (size_t index = 0U;
         index < measurement->response_length && offset + 2U < buffer_size;
         index++) {
        const int written = snprintf(buffer + offset,
                                     buffer_size - offset,
                                     "%02X",
                                     measurement->response[index]);
        if (written != 2) {
            break;
        }
        offset += 2U;
    }
    buffer[offset] = '\0';
}

void xd31h_format_diagnostic(const xd31h_measurement_t *measurement,
                             esp_err_t error,
                             char *buffer,
                             size_t buffer_size)
{
    if (buffer == NULL || buffer_size == 0U) {
        return;
    }
    if (measurement == NULL) {
        snprintf(buffer, buffer_size, "INVALID_ARGUMENT measurement=NULL");
        return;
    }

    char frame[(XD31H_MAX_RESPONSE_LENGTH * 2U) + 1U];
    xd31h_format_frame(measurement, frame, sizeof(frame));

    switch (measurement->diagnostic) {
    case XD31H_DIAGNOSTIC_INVALID_ARGUMENT:
        snprintf(buffer, buffer_size, "INVALID_ARGUMENT timeout_ms=0 error=%s",
                 esp_err_to_name(error));
        break;
    case XD31H_DIAGNOSTIC_NOT_INITIALIZED:
        snprintf(buffer, buffer_size, "NOT_INITIALIZED error=%s",
                 esp_err_to_name(error));
        break;
    case XD31H_DIAGNOSTIC_UART_FLUSH:
        snprintf(buffer, buffer_size, "UART_FLUSH error=%s",
                 esp_err_to_name(error));
        break;
    case XD31H_DIAGNOSTIC_UART_WRITE:
        snprintf(buffer, buffer_size,
                 "UART_WRITE written=%d expected=%u error=%s",
                 (int)measurement->diagnostic_value,
                 XD31H_REQUEST_LENGTH,
                 esp_err_to_name(error));
        break;
    case XD31H_DIAGNOSTIC_UART_TRANSMIT:
        snprintf(buffer, buffer_size, "UART_TRANSMIT error=%s",
                 esp_err_to_name(error));
        break;
    case XD31H_DIAGNOSTIC_UART_RECEIVE:
        snprintf(buffer, buffer_size,
                 "UART_RECEIVE error=%s received=%zu frame=%s",
                 esp_err_to_name(error), measurement->response_length, frame);
        break;
    case XD31H_DIAGNOSTIC_TIMEOUT:
        snprintf(buffer, buffer_size,
                 "TIMEOUT received=%zu frame=%s",
                 measurement->response_length, frame);
        break;
    case XD31H_DIAGNOSTIC_RESPONSE_TOO_LARGE:
        snprintf(buffer, buffer_size,
                 "RESPONSE_TOO_LARGE announced=%d capacity=%u frame=%s",
                 (int)measurement->diagnostic_value,
                 XD31H_MAX_RESPONSE_LENGTH,
                 frame);
        break;
    case XD31H_DIAGNOSTIC_FRAME_TOO_SHORT:
        snprintf(buffer, buffer_size,
                 "FRAME_TOO_SHORT length=%zu minimum=%u frame=%s",
                 measurement->response_length, XD31H_EXCEPTION_LENGTH, frame);
        break;
    case XD31H_DIAGNOSTIC_CRC_MISMATCH:
        snprintf(buffer, buffer_size,
                 "CRC_MISMATCH received=0x%04X calculated=0x%04X frame=%s",
                 measurement->received_crc,
                 measurement->calculated_crc,
                 frame);
        break;
    case XD31H_DIAGNOSTIC_SLAVE_ADDRESS:
        snprintf(buffer, buffer_size,
                 "SLAVE_ADDRESS actual=0x%02X expected=0x%02X frame=%s",
                 measurement->response[0], XD31H_SLAVE_ADDRESS, frame);
        break;
    case XD31H_DIAGNOSTIC_MODBUS_EXCEPTION:
        snprintf(buffer, buffer_size,
                 "MODBUS_EXCEPTION code=0x%02X name=%s frame=%s",
                 measurement->response[2],
                 xd31h_modbus_exception_name(measurement->response[2]),
                 frame);
        break;
    case XD31H_DIAGNOSTIC_RESPONSE_FORMAT:
        snprintf(buffer, buffer_size,
                 "RESPONSE_FORMAT length=%zu function=0x%02X byte_count=%u frame=%s",
                 measurement->response_length,
                 measurement->response_length > 1U ? measurement->response[1] : 0U,
                 measurement->response_length > 2U ? measurement->response[2] : 0U,
                 frame);
        break;
    case XD31H_DIAGNOSTIC_STATUS_OL:
        snprintf(buffer, buffer_size,
                 "OVERRANGE status=%u frame=%s",
                 measurement->status, frame);
        break;
    case XD31H_DIAGNOSTIC_STATUS_UNKNOWN:
        snprintf(buffer, buffer_size,
                 "STATUS_ERROR status=%u frame=%s",
                 measurement->status, frame);
        break;
    case XD31H_DIAGNOSTIC_INVALID_RANGE:
        snprintf(buffer, buffer_size,
                 "INVALID_RANGE range=%u raw=%u frame=%s",
                 measurement->range, measurement->raw_value, frame);
        break;
    case XD31H_DIAGNOSTIC_NONE:
    default:
        snprintf(buffer, buffer_size, "DRIVER_ERROR error=%s frame=%s",
                 esp_err_to_name(error), frame);
        break;
    }
}
