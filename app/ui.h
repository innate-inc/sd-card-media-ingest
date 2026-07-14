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

#endif /* INGEST_UI_H */
