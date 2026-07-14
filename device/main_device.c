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
#include <string.h>

#include "pico/stdlib.h"
#include "hardware/spi.h"

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

    for (;;) {
        pump_serial();
        lv_timer_handler();
        sleep_ms(5);
    }
}
