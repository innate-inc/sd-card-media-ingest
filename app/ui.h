/* Portable LVGL UI for the ingest display. Identical on device and simulator. */
#ifndef INGEST_UI_H
#define INGEST_UI_H

#include "model.h"

/* Logical screen size (landscape). The device rotates the 172x320 panel; the
 * simulator opens a window this size. */
#define UI_W 320
#define UI_H 172

/* Build the widget tree on the active LVGL screen and start its timers. */
void ui_create(void);

/* Replace the displayed model and refresh. */
void ui_update(const model_t *m);

/* Pulse the top-left activity pixel (call on each received line/heartbeat). */
void ui_heartbeat(void);

/* Single-button navigation (the RP2350 BOOTSEL button; a stdin key in the sim).
 * Callers that can time the press use down/up; up classifies short vs long.
 * Callers that already know the gesture call ui_button() directly. */
#define UI_BTN_SHORT 0
#define UI_BTN_LONG  1
void ui_button(int kind);
void ui_button_down(void);
void ui_button_up(void);

/* Invoked when the operator completes the arm+confirm wipe gesture on a card.
 * The device turns this into a `confirm <i>` line back to the host. */
typedef void (*ui_confirm_cb_t)(int slot);
void ui_set_confirm_cb(ui_confirm_cb_t cb);

#endif /* INGEST_UI_H */
