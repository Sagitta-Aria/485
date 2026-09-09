#pragma once   //预处理指令，当前头文件在一次编译的过程中只会被编译一次，防止重复包含

#include "driver/gpio.h"
#include "driver/uart.h"

/* The master matrix control lines are moved back to GPIO4-GPIO7. */
#define BOARD_CH446_U1_RST_GPIO     GPIO_NUM_4
#define BOARD_CH446_U1_DAT_GPIO     GPIO_NUM_5
#define BOARD_CH446_U1_CSCK_GPIO    GPIO_NUM_6
#define BOARD_CH446_U1_STB_GPIO     GPIO_NUM_7

/* UART1 keeps the original low-resistance connector and is now the RS485 bus. */
#define BOARD_RS485_UART_PORT       UART_NUM_1
#define BOARD_RS485_UART_TX_GPIO    GPIO_NUM_17
#define BOARD_RS485_UART_RX_GPIO    GPIO_NUM_18
#define BOARD_RS485_UART_BAUD_RATE  115200
#define BOARD_RS485_DE_RE_GPIO      GPIO_NUM_2

/* UART2 is moved to the former GPIO39-GPIO42 area; GPIO41/42 remain unused. */
#define BOARD_XD31H_UART_PORT       UART_NUM_2
#define BOARD_XD31H_UART_TX_GPIO    GPIO_NUM_40
#define BOARD_XD31H_UART_RX_GPIO    GPIO_NUM_39
#define BOARD_XD31H_UART_BAUD_RATE  9600
