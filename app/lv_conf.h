/* Minimal LVGL v9 config. Anything not set here falls back to the defaults in
 * lv_conf_internal.h. Shared by the device and the SDL simulator; the
 * simulator build defines LV_SIM to pull in the SDL backend. */
#ifndef LV_CONF_H
#define LV_CONF_H

#include <stdint.h>

/* 16-bit colour to match the ST7789 (RGB565). */
#define LV_COLOR_DEPTH 16

/* No hand-written NEON/Helium assembly (breaks the x86 simulator build). */
#define LV_USE_DRAW_SW_ASM LV_DRAW_SW_ASM_NONE

/* Built-in allocator. 48 KB is plenty for this small UI (RP2350 has 520 KB). */
#define LV_USE_STDLIB_MALLOC   LV_STDLIB_BUILTIN
#define LV_USE_STDLIB_STRING   LV_STDLIB_BUILTIN
#define LV_USE_STDLIB_SPRINTF  LV_STDLIB_BUILTIN
#ifdef LV_SIM
#define LV_MEM_SIZE (1024 * 1024U)   /* snapshot needs room on the desktop */
#else
#define LV_MEM_SIZE (48 * 1024U)     /* device: plenty for this small UI */
#endif

/* We drive lv_tick ourselves (SDL ticks / board time) via lv_tick_set_cb(). */
#define LV_USE_LOG 0

/* Snapshot: used by the simulator's --shot mode to dump a PNG for testing. */
#define LV_USE_SNAPSHOT 1

/* Fonts we use. */
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14

/* Simulator: SDL window backend (device build leaves LV_SIM undefined). */
#ifdef LV_SIM
#define LV_USE_SDL 1
#define LV_SDL_INCLUDE_PATH <SDL2/SDL.h>
#endif

#endif /* LV_CONF_H */
