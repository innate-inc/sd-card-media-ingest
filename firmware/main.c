/*****************************************************************************
 * RP2350-LCD-1.47 USB serial image display firmware
 *
 * Receives framed RGB565 images over the USB CDC serial port and blits them
 * to the onboard 1.47" 172x320 ST7789V3 LCD.
 *
 * Wire protocol (little-endian header, see DESIGN.md for the full spec):
 *
 *   offset  size  field
 *   0       4     magic   = "WSI1"  (0x57 0x53 0x49 0x31)
 *   4       1     format  = 0x00    (RGB565, big-endian per pixel)
 *   5       1     flags   = 0x00    (reserved, must be 0)
 *   6       2     width   (uint16 LE)   1..172
 *   8       2     height  (uint16 LE)   1..320
 *   10      2     reserved (uint16 LE, must be 0)
 *   12      W*H*2 pixel payload, row-major, top-left origin,
 *                 2 bytes/pixel: high byte first (matches ST7789 RAMWR order).
 *
 * After a frame is drawn the firmware writes "OK <w> <h>\r\n" back on the same
 * serial port; on any error it writes "ERR <reason>\r\n" and resynchronises by
 * hunting for the next magic marker.
 *
 * The LCD low-level driver (DEV_Config.*, LCD_1in47.*) is Waveshare's own
 * MIT-licensed reference code, vendored unmodified under firmware/lib/.
 *****************************************************************************/
#include <stdio.h>
#include <string.h>
#include <stdint.h>

#include "pico/stdlib.h"
#include "hardware/spi.h"

#include "DEV_Config.h"
#include "LCD_1in47.h"

/* Panel geometry in the orientation we drive it (VERTICAL = portrait). */
#define PANEL_W 172
#define PANEL_H 320
#define BYTES_PER_PIXEL 2
#define MAX_PAYLOAD (PANEL_W * PANEL_H * BYTES_PER_PIXEL)

/* Protocol constants. */
static const uint8_t MAGIC[4] = {'W', 'S', 'I', '1'};
#define HEADER_LEN 12
#define FMT_RGB565_BE 0x00

/* Per-byte inactivity timeout while a frame is in flight (microseconds).
 * USB 1.1 full speed moves a full frame (~110 KB) in well under a second, so a
 * generous 2 s per byte only ever trips on a genuinely stalled/aborted host. */
#define BYTE_TIMEOUT_US (2u * 1000u * 1000u)

/* One full panel's worth of pixels. Stored exactly as received (big-endian
 * RGB565) so it can be streamed straight to the ST7789 with no per-pixel work.
 * 110,080 bytes out of the RP2350's 520 KB SRAM. */
static uint8_t framebuf[MAX_PAYLOAD];

/* --- serial helpers -------------------------------------------------------*/

/* Read exactly `len` bytes into `dst`, giving up if any single byte fails to
 * arrive within BYTE_TIMEOUT_US. Returns true on success. */
static bool read_exact(uint8_t *dst, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        int c = getchar_timeout_us(BYTE_TIMEOUT_US);
        if (c == PICO_ERROR_TIMEOUT) {
            return false;
        }
        dst[i] = (uint8_t)c;
    }
    return true;
}

/* Block until the 4-byte magic marker has been seen. Uses a sliding window so
 * a partial/garbled prefix cannot wedge the parser. No timeout: when idle we
 * simply wait here for the next frame. */
static void wait_for_magic(void)
{
    size_t matched = 0;
    while (matched < sizeof(MAGIC)) {
        int c = getchar_timeout_us(BYTE_TIMEOUT_US);
        if (c == PICO_ERROR_TIMEOUT) {
            continue; /* stay idle, keep listening */
        }
        if ((uint8_t)c == MAGIC[matched]) {
            matched++;
        } else {
            /* Restart, but treat this byte as a possible new start-of-magic. */
            matched = ((uint8_t)c == MAGIC[0]) ? 1 : 0;
        }
    }
}

/* --- LCD helpers ----------------------------------------------------------*/

/* Push a (already big-endian RGB565) buffer covering the rectangle
 * (0,0)-(w,h) to the panel. Reuses Waveshare's window/offset logic so the
 * 172x320 memory offset is handled correctly. */
static void blit(const uint8_t *pixels, uint16_t w, uint16_t h)
{
    LCD_1IN47_SetWindows(0, 0, w, h); /* also issues RAMWR (0x2C) */
    DEV_Digital_Write(LCD_DC_PIN, 1);
    DEV_Digital_Write(LCD_CS_PIN, 0);
    DEV_SPI_Write_nByte((uint8_t *)pixels, (uint32_t)w * h * BYTES_PER_PIXEL);
    DEV_Digital_Write(LCD_CS_PIN, 1);
}

/* Fill the whole panel with a solid big-endian RGB565 colour. */
static void fill_panel(uint16_t color_be)
{
    for (size_t i = 0; i < MAX_PAYLOAD; i += 2) {
        framebuf[i] = (uint8_t)(color_be >> 8);
        framebuf[i + 1] = (uint8_t)(color_be & 0xFF);
    }
    blit(framebuf, PANEL_W, PANEL_H);
}

/* Boot splash: three vertical R/G/B bars, so the panel visibly proves itself
 * alive before the host ever sends a frame. Colours are big-endian RGB565. */
static void boot_splash(void)
{
    const uint16_t red = 0xF800, green = 0x07E0, blue = 0x001F;
    for (uint16_t y = 0; y < PANEL_H; y++) {
        for (uint16_t x = 0; x < PANEL_W; x++) {
            uint16_t c = (x < PANEL_W / 3) ? red
                       : (x < 2 * PANEL_W / 3) ? green
                       : blue;
            size_t o = (size_t)(y * PANEL_W + x) * 2;
            framebuf[o] = (uint8_t)(c >> 8);
            framebuf[o + 1] = (uint8_t)(c & 0xFF);
        }
    }
    blit(framebuf, PANEL_W, PANEL_H);
}

/* --- main -----------------------------------------------------------------*/

int main(void)
{
    /* DEV_Module_Init() calls stdio_init_all(), which brings up the USB CDC
     * serial interface, and configures SPI0 + the PWM backlight. */
    DEV_Module_Init();
    /* NB: Waveshare's scan-direction names are counter-intuitive. HORIZONTAL
     * gives the portrait 172(W) x 320(H) framebuffer we want, with the ST7789
     * column offset applied to the 172-pixel axis. (VERTICAL would be a 320x172
     * landscape frame and would leave a 172-wide image in the left half.) */
    LCD_1IN47_Init(HORIZONTAL);

    /* NB: we deliberately do NOT call LCD_1IN47_Clear() -- Waveshare's version
     * allocates a full-panel framebuffer (110 KB) as a stack VLA, which would
     * overflow the RP2350's default 2 KB stack. boot_splash() paints the whole
     * panel from our static buffer instead. */
    boot_splash();

    for (;;) {
        wait_for_magic();

        /* Read the rest of the header (magic already consumed). */
        uint8_t hdr[HEADER_LEN - 4];
        if (!read_exact(hdr, sizeof(hdr))) {
            printf("ERR header-timeout\r\n");
            continue;
        }

        uint8_t format = hdr[0];
        uint8_t flags = hdr[1];
        uint16_t w = (uint16_t)hdr[2] | ((uint16_t)hdr[3] << 8);
        uint16_t h = (uint16_t)hdr[4] | ((uint16_t)hdr[5] << 8);
        uint16_t reserved = (uint16_t)hdr[6] | ((uint16_t)hdr[7] << 8);

        if (format != FMT_RGB565_BE || flags != 0 || reserved != 0) {
            printf("ERR bad-header\r\n");
            continue;
        }
        if (w == 0 || h == 0 || w > PANEL_W || h > PANEL_H) {
            printf("ERR bad-size %u %u\r\n", w, h);
            continue;
        }

        uint32_t payload = (uint32_t)w * h * BYTES_PER_PIXEL;
        if (!read_exact(framebuf, payload)) {
            printf("ERR payload-timeout\r\n");
            continue;
        }

        blit(framebuf, w, h);
        printf("OK %u %u\r\n", w, h);
    }
}
