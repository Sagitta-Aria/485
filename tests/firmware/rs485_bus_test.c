/* Drive the production RS485 framing layer with a scripted byte stream.
 *
 * The stub clock advances one 10 ms tick per simulated "read attempt", which is
 * how the real code behaves on an idle line: uart_read_bytes() blocks for its
 * whole wait and returns nothing. Bytes carry an arrival tick so the harness can
 * place them inside or outside the inter-frame gap.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "../../master/main/rs485_bus.c"

#define CHECK(condition) do { if (!(condition)) return __LINE__; } while (0)

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

struct stream_byte { TickType_t at; uint8_t value; };

static struct stream_byte stream[512];
static size_t stream_count, stream_read;
static TickType_t now, elapsed_ticks;
/* Reads whose wait equals the one-tick silence probe: the inter-frame check. */
static unsigned gap_probes;

TickType_t xTaskGetTickCount(void) { return now; }

int uart_read_bytes(uart_port_t port, void *buffer, uint32_t length, TickType_t wait)
{
    (void)port;
    if (wait == rs485_silence_ticks()) ++gap_probes;
    /* Ordered as the real driver is: one non-blocking read first, then blocking
     * only while the wait allows. A zero wait must still drain what is queued,
     * otherwise a deadline that lands exactly on a tick would look like a
     * timeout even though the bytes are already there. */
    size_t elapsed_this_call = 0;
    while (true) {
        uint8_t *bytes = buffer;
        uint32_t taken = 0;
        while (taken < length && stream_read < stream_count && stream[stream_read].at <= now) {
            bytes[taken++] = stream[stream_read++].value;
        }
        if (taken > 0) return (int)taken;
        if (elapsed_this_call >= wait) return 0;
        ++now;
        ++elapsed_ticks;
        ++elapsed_this_call;
    }
}

int uart_write_bytes(uart_port_t port, const void *source, size_t length)
{
    (void)port; (void)source;
    return (int)length;
}
esp_err_t uart_wait_tx_done(uart_port_t port, TickType_t wait) { (void)port; (void)wait; return ESP_OK; }
esp_err_t uart_flush_input(uart_port_t port) { (void)port; return ESP_OK; }
esp_err_t uart_param_config(uart_port_t port, const uart_config_t *config) { (void)port; (void)config; return ESP_OK; }
esp_err_t uart_set_pin(uart_port_t port, int tx, int rx, int rts, int cts) { (void)port; (void)tx; (void)rx; (void)rts; (void)cts; return ESP_OK; }
esp_err_t uart_driver_install(uart_port_t port, int rx_buffer, int tx_buffer, int queue, void *handle, int flags)
{ (void)port; (void)rx_buffer; (void)tx_buffer; (void)queue; (void)handle; (void)flags; return ESP_OK; }
esp_err_t uart_driver_delete(uart_port_t port) { (void)port; return ESP_OK; }
esp_err_t gpio_config(const gpio_config_t *config) { (void)config; return ESP_OK; }
esp_err_t gpio_set_level(gpio_num_t gpio, unsigned level) { (void)gpio; (void)level; return ESP_OK; }
const char *esp_err_to_name(esp_err_t error) { (void)error; return "err"; }
struct fake_mutex { bool locked; };
static struct fake_mutex mutex;
SemaphoreHandle_t xSemaphoreCreateRecursiveMutex(void) { return &mutex; }
BaseType_t xSemaphoreTakeRecursive(SemaphoreHandle_t semaphore, TickType_t wait) { (void)semaphore; (void)wait; return pdTRUE; }
void xSemaphoreGiveRecursive(SemaphoreHandle_t semaphore) { (void)semaphore; }

static int init_ok;

static esp_err_t receive(rs485_frame_t *frame, uint32_t timeout_ms)
{
    return rs485_bus_receive(frame, timeout_ms);
}

static void reset_stream(void)
{
    stream_count = stream_read = 0;
    now = elapsed_ticks = 0;
    gap_probes = 0U;
    /* rs485_bus_receive() rejects everything until the bus is initialized, and
     * every stub below succeeds, so init also proves the driver wiring runs. */
    s_initialized = false;
    init_ok = rs485_bus_init() == ESP_OK;
}

static void push_at(TickType_t at, const uint8_t *bytes, size_t length)
{
    for (size_t index = 0; index < length; index++) {
        stream[stream_count].at = at;
        stream[stream_count].value = bytes[index];
        ++stream_count;
    }
}

/* Build one framed request exactly as rs485_bus_send() would put it on the wire. */
static size_t frame(uint8_t *out, uint8_t address, uint8_t command,
                    const uint8_t *payload, uint8_t length)
{
    out[0] = RS485_SOF;
    out[1] = address;
    out[2] = command;
    out[3] = length;
    if (length) memcpy(&out[4], payload, length);
    const size_t body = 4U + length;
    const uint16_t crc = rs485_crc16(out, body);
    out[body] = (uint8_t)(crc & 0xFFU);
    out[body + 1U] = (uint8_t)(crc >> 8U);
    return body + 2U;
}

EXPORT int rs485_bus_test_gap_separated_frames(void)
{
    uint8_t wire[64];
    const size_t length = frame(wire, 0x11U, RS485_CMD_PING, NULL, 0U);
    rs485_frame_t received;

    reset_stream();
    CHECK(init_ok);
    CHECK(gap_probes == 0U);
    push_at(0U, wire, length);
    CHECK(receive(&received, 50U) == ESP_OK);
    CHECK(received.address == 0x11U && received.command == RS485_CMD_PING && received.length == 0U);
    CHECK(stream_read == stream_count);
    /* The delivered frame must have been confirmed by an idle line. */
    CHECK(gap_probes == 1U);

    /* Two frames back to back must each still be delivered. The second frame
     * starts a full gap after the first one ended, which is the closest spacing
     * a sender can produce, and both must arrive intact. */
    reset_stream();
    CHECK(init_ok);
    push_at(0U, wire, length);
    push_at(7U, wire, length);
    CHECK(receive(&received, 200U) == ESP_OK);
    CHECK(receive(&received, 200U) == ESP_OK);
    CHECK(received.address == 0x11U && stream_read == stream_count);
    CHECK(gap_probes == 2U);
    return 0;
}

/* An intact frame after non-frame noise, plus the recovery contract for a burst
 * whose 0xA5 run turns out to be too short to be a frame. */
EXPORT int rs485_bus_test_noise_then_frame(void)
{
    uint8_t wire[64];
    const size_t length = frame(wire, 0x11U, RS485_CMD_STATUS, NULL, 0U);
    rs485_frame_t received;

    reset_stream();
    CHECK(init_ok);
    /* No 0xA5 anywhere, so the scanner must consume all of it and still reach
     * the real frame that follows. */
    const uint8_t noise[] = { 0x00U, 0x7FU, 0x03U, 0x91U, 0x22U, 0x5CU };
    push_at(0U, noise, sizeof(noise));
    push_at(3U, wire, length);
    CHECK(receive(&received, 200U) == ESP_OK);
    CHECK(received.address == 0x11U && received.command == RS485_CMD_STATUS);
    CHECK(received.length == 0U);
    CHECK(stream_read == stream_count);
    CHECK(gap_probes == 1U);

    /* A run that starts like a frame but announces more bytes than the frame
     * format allows is rejected, and the caller retries. */
    reset_stream();
    CHECK(init_ok);
    const uint8_t truncated[] = { 0x00U, RS485_SOF, 0x11U, 0x02U, 0x91U };
    push_at(0U, truncated, sizeof(truncated));
    CHECK(receive(&received, 50U) == ESP_ERR_INVALID_SIZE);
    return 0;
}

/* The regression this check exists for: without the idle check the parser
 * returns the CRC-valid frame sitting inside a noise burst, and the caller then
 * reads the remaining noise as if it were the start of the next frame. */
EXPORT int rs485_bus_test_gap_rejects_embedded_frame(void)
{
    uint8_t embedded[64];
    const size_t length = frame(embedded, 0x11U, RS485_CMD_PING, NULL, 0U);
    rs485_frame_t received;
    uint8_t burst[64];
    size_t total = 0U;

    /* The burst is exactly one CRC-valid frame plus a stray byte in the gap:
     * nothing before it can be mistaken for a frame start, so the only thing
     * standing between the caller and this fragment is the idle check. */
    memcpy(&burst[total], embedded, length);
    total += length;
    burst[total++] = 0x00U;

    reset_stream();
    CHECK(init_ok);
    push_at(0U, burst, total);
    CHECK(receive(&received, 200U) == ESP_ERR_INVALID_CRC);

    /* The same frame is accepted once it really is the last thing on the wire. */
    reset_stream();
    CHECK(init_ok);
    push_at(0U, embedded, length);
    CHECK(receive(&received, 200U) == ESP_OK);
    CHECK(received.address == 0x11U && received.command == RS485_CMD_PING);
    CHECK(gap_probes == 1U);
    return 0;
}

/* Adding the idle check must not let one call run past the requested timeout.
 * The scan loop here never completes a frame, so this check stays a pure
 * deadline regression; the probe itself is pinned by the checks above. */
EXPORT int rs485_bus_test_gap_respects_deadline(void)
{
    rs485_frame_t received;
    reset_stream();
    CHECK(init_ok);
    const uint8_t noise[] = { 0x00U, RS485_SOF, 0x7FU };
    push_at(0U, noise, sizeof(noise));
    CHECK(receive(&received, 50U) == ESP_ERR_TIMEOUT);
    CHECK(elapsed_ticks <= pdMS_TO_TICKS(50U) + 1U);
    return 0;
}
