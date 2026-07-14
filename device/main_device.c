/* RP2350-LCD-1.47 device firmware: runs the shared LVGL UI (app/) on the
 * onboard ST7789 panel, driven by the serial line protocol over USB-CDC.
 *
 * The panel is initialised in Waveshare's VERTICAL scan direction, which
 * addresses it as landscape 320(W) x 172(H) with the ST7789 column offset on
 * the correct axis -- so LVGL renders 320x172 directly with no rotation. LVGL
 * stores RGB565 little-endian; the ST7789 wants big-endian, so each flushed
 * span is byte-swapped before it goes out on SPI.
 */
#include <stdio.h>

#include "pico/stdlib.h"
#include "hardware/spi.h"
#include "hardware/sync.h"
#include "hardware/structs/ioqspi.h"
#include "hardware/structs/sio.h"

#include "lvgl.h"

#include "DEV_Config.h"
#include "LCD_1in47.h"
#include "ui.h"
#include "proto.h"

/* Landscape drawing area (matches UI_W x UI_H in app/ui.h). */
#define DISP_W 320
#define DISP_H 172

static model_t model;

static uint32_t tick_cb(void) {
    return to_ms_since_boot(get_absolute_time());
}

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px) {
    int w = area->x2 - area->x1 + 1;
    int h = area->y2 - area->y1 + 1;
    lv_draw_sw_rgb565_swap(px, (uint32_t)w * h);
    LCD_1IN47_SetWindows(area->x1, area->y1, area->x2 + 1, area->y2 + 1);
    DEV_Digital_Write(LCD_DC_PIN, 1);
    DEV_Digital_Write(LCD_CS_PIN, 0);
    DEV_SPI_Write_nByte(px, (uint32_t)w * h * 2);
    DEV_Digital_Write(LCD_CS_PIN, 1);
    lv_display_flush_ready(disp);
}

/* Sample the BOOTSEL button at runtime. It doubles as the QSPI chip-select, so
 * we tri-state CS, read the pad, and restore it with interrupts off -- and the
 * whole routine must run from RAM (never flash, whose access we just disabled).
 * (Adapted from the Pico SDK picoboard/button example.) */
static bool __no_inline_not_in_flash_func(bootsel_pressed)(void) {
    const uint CS_IDX = 1;
    uint32_t flags = save_and_disable_interrupts();
    hw_write_masked(&ioqspi_hw->io[CS_IDX].ctrl,
                    GPIO_OVERRIDE_LOW << IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_LSB,
                    IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_BITS);
    for (volatile int i = 0; i < 1000; ++i) { }
#if PICO_RP2040
    const uint32_t cs_bit = 1u << 1;
#else
    const uint32_t cs_bit = SIO_GPIO_HI_IN_QSPI_CSN_BITS;
#endif
    bool pressed = !(sio_hw->gpio_hi_in & cs_bit);   /* active-low */
    hw_write_masked(&ioqspi_hw->io[CS_IDX].ctrl,
                    GPIO_OVERRIDE_NORMAL << IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_LSB,
                    IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_BITS);
    restore_interrupts(flags);
    return pressed;
}

/* Debounce the button and turn stable edges into ui_button_down/up. */
static void poll_button(void) {
    static bool stable = false;
    static int agree = 0;
    bool raw = bootsel_pressed();
    if (raw == stable) { agree = 0; return; }
    if (++agree < 3) return;            /* need a few consistent reads */
    agree = 0;
    stable = raw;
    if (stable) ui_button_down();
    else        ui_button_up();
}

/* The operator confirmed a wipe: tell the host over the same USB-CDC link. */
static void on_confirm(int slot) {
    printf("confirm %d\n", slot);
}

/* Read whatever serial bytes are buffered, dispatch complete lines. */
static void pump_serial(void) {
    static char buf[256];
    static int len = 0;
    int c;
    while ((c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
        if (c == '\n') {
            buf[len] = '\0';
            proto_result_t r = proto_handle_line(&model, buf);
            if (r.heartbeat) ui_heartbeat();
            if (r.changed) ui_update(&model);
            len = 0;
        } else if (len < (int)sizeof(buf) - 1) {
            buf[len++] = (char)c;
        }
    }
}

int main(void) {
    DEV_Module_Init();            /* stdio_usb + SPI0 + PWM backlight */
    LCD_1IN47_Init(VERTICAL);     /* landscape 320x172 addressing */

    lv_init();
    lv_tick_set_cb(tick_cb);

    lv_display_t *disp = lv_display_create(DISP_W, DISP_H);
    static uint8_t draw_buf[DISP_W * 40 * 2];   /* ~25 KB partial buffer */
    lv_display_set_buffers(disp, draw_buf, NULL, sizeof(draw_buf),
                           LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(disp, flush_cb);

    model_init(&model);
    ui_create();
    ui_set_confirm_cb(on_confirm);

    for (;;) {
        pump_serial();
        poll_button();
        lv_timer_handler();
        sleep_ms(5);
    }
}
