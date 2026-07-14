/* Desktop simulator: runs the exact same LVGL UI as the device, in an SDL
 * window. Serial input is read from stdin (same line protocol as the board),
 * so you can drive it with the same feeder:
 *
 *     printf 'slot 0 238000 900 active 300 22c35e 200 0072b2 250 e69f00 0 0 SANDISK\n' | ingest-sim
 *
 * --shot <ms> <file.ppm> renders headless for <ms> then dumps a snapshot and
 * exits (used by the test harness; needs no visible display under xvfb).
 */
#define _POSIX_C_SOURCE 200809L
#define SDL_MAIN_HANDLED          /* we keep our own main() */
#include <SDL2/SDL.h>

#include "lvgl.h"
#include "ui.h"
#include "proto.h"

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static model_t model;

/* The operator confirmed a wipe: emit the device->host confirm line. */
static void on_confirm(int slot) {
    printf("confirm %d\n", slot);
    fflush(stdout);
}

/* Interactive input: SPACE stands in for the board's one button. We read the
 * key *state* (not events) each frame and derive down/up edges, so LVGL's own
 * SDL event pump and ours don't fight over the queue; holding SPACE past
 * LONG_MS registers as a long press exactly like the hardware. ESC quits. */
static void poll_keyboard(void) {
    static int prev = 0;
    SDL_PumpEvents();
    const Uint8 *ks = SDL_GetKeyboardState(NULL);
    if (ks[SDL_SCANCODE_ESCAPE]) exit(0);
    int now = ks[SDL_SCANCODE_SPACE];
    if (now && !prev) ui_button_down();
    else if (!now && prev) ui_button_up();
    prev = now;
}

static void dummy_flush(lv_display_t *d, const lv_area_t *a, uint8_t *px) {
    (void)a; (void)px;
    lv_display_flush_ready(d);
}

static uint32_t tick_cb(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static void pump_stdin(void) {
    static char buf[512];
    static int len = 0;
    struct pollfd p = {.fd = 0, .events = POLLIN};
    while (poll(&p, 1, 0) > 0 && (p.revents & POLLIN)) {
        char c;
        ssize_t r = read(0, &c, 1);
        if (r <= 0) return;
        if (c == '\n') {
            if (len > 0 && buf[len - 1] == '\r') len--;   /* tolerate CRLF */
            buf[len] = '\0';
            /* sim-only: fake the board's one button from stdin */
            if (!strcmp(buf, "press short")) {
                ui_button(UI_BTN_SHORT);
            } else if (!strcmp(buf, "press long")) {
                ui_button(UI_BTN_LONG);
            } else if (!strcmp(buf, "press down")) {
                ui_button_down();
            } else if (!strcmp(buf, "press up")) {
                ui_button_up();
            } else {
                proto_result_t res = proto_handle_line(&model, buf);
                if (res.heartbeat) ui_heartbeat();
                if (res.changed) ui_update(&model);
            }
            len = 0;
        } else if (len < (int)sizeof(buf) - 1) {
            buf[len++] = c;
        }
    }
}

static void write_ppm(const char *path) {
    lv_draw_buf_t *snap = lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_RGB888);
    if (!snap) { fprintf(stderr, "snapshot failed\n"); return; }
    int w = snap->header.w, h = snap->header.h;
    uint32_t stride = snap->header.stride;
    FILE *f = fopen(path, "wb");
    if (!f) { lv_draw_buf_destroy(snap); return; }
    fprintf(f, "P6\n%d %d\n255\n", w, h);
    for (int y = 0; y < h; y++) {
        const uint8_t *row = snap->data + (size_t)y * stride;
        for (int x = 0; x < w; x++) {
            /* LVGL RGB888 is stored B,G,R in memory. */
            const uint8_t *px = row + x * 3;
            fputc(px[2], f); fputc(px[1], f); fputc(px[0], f);
        }
    }
    fclose(f);
    lv_draw_buf_destroy(snap);
    fprintf(stderr, "wrote %s\n", path);
}

int main(int argc, char **argv) {
    int shot_ms = 0;
    const char *shot_file = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--shot") && i + 2 < argc) {
            shot_ms = atoi(argv[i + 1]);
            shot_file = argv[i + 2];
            i += 2;
        }
    }

    lv_init();
    lv_tick_set_cb(tick_cb);

    if (shot_file) {
        /* Headless: a dummy display so snapshot works with no window/X server. */
        static uint8_t dbuf[UI_W * 40 * 2];
        lv_display_t *d = lv_display_create(UI_W, UI_H);
        lv_display_set_buffers(d, dbuf, NULL, sizeof(dbuf),
                               LV_DISPLAY_RENDER_MODE_PARTIAL);
        lv_display_set_flush_cb(d, dummy_flush);
    } else {
        lv_sdl_window_create(UI_W, UI_H);
        fprintf(stderr, "ingest-sim: SPACE = button (hold = long press), ESC = quit\n");
    }

    model_init(&model);
    ui_create();
    ui_set_confirm_cb(on_confirm);

    uint32_t start = tick_cb();
    for (;;) {
        pump_stdin();
        if (!shot_file) poll_keyboard();
        uint32_t idle = lv_timer_handler();
        if (shot_file && tick_cb() - start >= (uint32_t)shot_ms) {
            write_ppm(shot_file);
            break;
        }
        if (idle > 30 || idle == LV_NO_TIMER_READY) idle = 30;
        struct timespec ts = {0, (long)idle * 1000000L};
        nanosleep(&ts, NULL);
    }
    return 0;
}
