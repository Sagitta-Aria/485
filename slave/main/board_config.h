#pragma once   //预处理指令，当前头文件在一次编译的过程中只会被编译一次，防止重复包含

#include "driver/gpio.h"
#include "driver/uart.h"

/* CH446X U1 control pins from the current teacher schematic. */
#define BOARD_CH446_U1_RST_GPIO     GPIO_NUM_4
#define BOARD_CH446_U1_DAT_GPIO     GPIO_NUM_5
#define BOARD_CH446_U1_CSCK_GPIO    GPIO_NUM_6
#define BOARD_CH446_U1_STB_GPIO     GPIO_NUM_7

/* CH446X U2 control pins. GPIO39-GPIO42 also have JTAG functions. */
#define BOARD_CH446_U2_RST_GPIO     GPIO_NUM_42
#define BOARD_CH446_U2_DAT_GPIO     GPIO_NUM_41
#define BOARD_CH446_U2_CSCK_GPIO    GPIO_NUM_40
#define BOARD_CH446_U2_STB_GPIO     GPIO_NUM_39

/* UART1 keeps the original low-resistance connector and is now the RS485 bus. */
#define BOARD_RS485_UART_PORT       UART_NUM_1
#define BOARD_RS485_UART_TX_GPIO    GPIO_NUM_17
#define BOARD_RS485_UART_RX_GPIO    GPIO_NUM_18
#define BOARD_RS485_UART_BAUD_RATE  115200
#define BOARD_RS485_DE_RE_GPIO      GPIO_NUM_2

/* Set these two decimal indices for each board. Side affects Wi-Fi only;
 * the local slave index determines the independent RS485 address on that side.
 */
#ifndef BOARD_SLAVE_MASTER_INDEX
#define BOARD_SLAVE_MASTER_INDEX    1
#endif
#ifndef BOARD_SLAVE_INDEX
#define BOARD_SLAVE_INDEX           1
#endif
#if BOARD_SLAVE_MASTER_INDEX < 1 || BOARD_SLAVE_MASTER_INDEX > 2
#error "BOARD_SLAVE_MASTER_INDEX must be 1 or 2"
#endif
#if BOARD_SLAVE_INDEX < 1 || BOARD_SLAVE_INDEX > 10
#error "BOARD_SLAVE_INDEX must be between 1 and 10"
#endif

#define BOARD_RS485_NODE_ID         (0x10U + BOARD_SLAVE_INDEX)
#define BOARD_ID_TEXT_IMPL(value)   #value
#define BOARD_ID_TEXT(value)        BOARD_ID_TEXT_IMPL(value)
#define BOARD_SLAVE_WIFI_DEVICE_ID  "m" BOARD_ID_TEXT(BOARD_SLAVE_MASTER_INDEX) \
                                   "-s" BOARD_ID_TEXT(BOARD_SLAVE_INDEX)

/* Wi-Fi is optional for debugging; RS485 remains active in either setting. */
#define BOARD_SLAVE_WIFI_DEBUG_ENABLED 1
#define BOARD_SLAVE_MASK_LEASE_MS      30000U
