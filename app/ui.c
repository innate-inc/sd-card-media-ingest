#include "ui.h"

#include "lvgl.h"

#include <stdio.h>
#include <string.h>

#define COLS_PER_PAGE 4
#define PAGE_MS   4000   /* cycle pages when there are more than 4 devices */
#define TOGGLE_MS 2000   /* 0.5 Hz: name <-> eta/size */
#define HB_MS      180   /* how long the heartbeat pixel stays lit */

#define MARGIN_X 6
#define TOP_Y    12
#define BOT_Y    (UI_H - 6)
#define COL_GAP  4
#define COL_H    (BOT_Y - TOP_Y)
#define COL_W    ((UI_W - 2 * MARGIN_X - (COLS_PER_PAGE - 1) * COL_GAP) / COLS_PER_PAGE)

#define C_TEXT   lv_color_hex(0xFFFFFF)
#define C_HB_ON  lv_color_hex(0xFFFFFF)
#define C_HB_OFF lv_color_hex(0x303030)

typedef struct {
    lv_obj_t *col;                 /* background = empty colour */
    lv_obj_t *seg[MAX_SEGS];       /* stacked segment rects */
    lv_obj_t *num[MAX_SEGS];       /* per-segment numbers (optional) */
    lv_obj_t *name;
    lv_obj_t *pct;
} column_t;

static struct {
    model_t model;
    column_t cols[COLS_PER_PAGE];
    lv_obj_t *waiting;
    lv_obj_t *hb;
    uint32_t hb_last;
    int hb_seen;
    int phase;
    int page;
} g;

static void fmt_eta(char *buf, size_t n, int eta) {
    if (eta < 0) { buf[0] = '\0'; return; }
    if (eta >= 3600) snprintf(buf, n, "%.1fh", eta / 3600.0);
    else if (eta >= 60) snprintf(buf, n, "%dm", (eta + 30) / 60);
    else snprintf(buf, n, "%ds", eta);
}

static void fmt_gb(char *buf, size_t n, double gb) {
    if (gb >= 1000) snprintf(buf, n, "%.1fT", gb / 1000.0);
    else if (gb >= 100) snprintf(buf, n, "%.0fG", gb);
    else snprintf(buf, n, "%.1fG", gb);
}

static int page_count(void) {
    int total = g.model.count;
    if (total <= 0) return 1;
    return (total + COLS_PER_PAGE - 1) / COLS_PER_PAGE;
}

static void refresh(void) {
    int total = g.model.count;

    if (total <= 0) {
        lv_obj_clear_flag(g.waiting, LV_OBJ_FLAG_HIDDEN);
        for (int i = 0; i < COLS_PER_PAGE; i++)
            lv_obj_add_flag(g.cols[i].col, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    lv_obj_add_flag(g.waiting, LV_OBJ_FLAG_HIDDEN);

    if (g.page >= page_count()) g.page = 0;
    int base = g.page * COLS_PER_PAGE;

    for (int i = 0; i < COLS_PER_PAGE; i++) {
        column_t *c = &g.cols[i];
        int si = base + i;
        int shown = si < total;
        lv_obj_t *toggle[] = {c->col, c->name, c->pct};
        for (unsigned k = 0; k < 3; k++) {
            if (shown) lv_obj_clear_flag(toggle[k], LV_OBJ_FLAG_HIDDEN);
            else lv_obj_add_flag(toggle[k], LV_OBJ_FLAG_HIDDEN);
        }
        if (!shown) {
            for (int k = 0; k < MAX_SEGS; k++)
                lv_obj_add_flag(c->num[k], LV_OBJ_FLAG_HIDDEN);
            continue;
        }

        slot_t *s = &g.model.slots[si];
        int x0 = MARGIN_X + i * (COL_W + COL_GAP);
        lv_obj_set_style_bg_color(c->col, lv_color_hex(g.model.empty_color), 0);

        /* stack segments from the bottom */
        int accH = 0, filled_pm = 0;
        for (int k = 0; k < MAX_SEGS; k++) {
            int pm = s->segs[k].permille;
            int h = (COL_H * pm) / 1000;
            if (h <= 0) {
                lv_obj_add_flag(c->seg[k], LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(c->num[k], LV_OBJ_FLAG_HIDDEN);
                continue;
            }
            int ytop = COL_H - (accH + h);
            lv_obj_clear_flag(c->seg[k], LV_OBJ_FLAG_HIDDEN);
            lv_obj_set_pos(c->seg[k], 0, ytop);
            lv_obj_set_size(c->seg[k], COL_W, h);
            lv_obj_set_style_bg_color(c->seg[k], lv_color_hex(s->segs[k].color), 0);

            if (g.model.show_numbers && h >= 12) {
                char nb[16];
                if (s->size_mb >= 0)
                    fmt_gb(nb, sizeof nb, s->size_mb / 1000.0 * pm / 1000.0);
                else
                    snprintf(nb, sizeof nb, "%d%%", (pm + 5) / 10);
                lv_label_set_text(c->num[k], nb);
                lv_obj_set_width(c->num[k], COL_W);
                lv_obj_set_pos(c->num[k], x0, TOP_Y + ytop + h / 2 - 7);
                lv_obj_clear_flag(c->num[k], LV_OBJ_FLAG_HIDDEN);
            } else {
                lv_obj_add_flag(c->num[k], LV_OBJ_FLAG_HIDDEN);
            }
            accH += h;
            filled_pm += pm;
        }

        char buf[48];
        if (g.phase == 1) {
            char eta[16], size[16];
            fmt_eta(eta, sizeof eta, s->eta_s);
            if (s->size_mb >= 0) fmt_gb(size, sizeof size, s->size_mb / 1000.0);
            else size[0] = '\0';
            if (s->status == ST_DONE) snprintf(buf, sizeof buf, "done");
            else if (eta[0] && size[0]) snprintf(buf, sizeof buf, "%s %s", eta, size);
            else if (eta[0] || size[0]) snprintf(buf, sizeof buf, "%s%s", eta, size);
            else snprintf(buf, sizeof buf, "%d %s", si + 1, s->label);
        } else {
            snprintf(buf, sizeof buf, "%d %s", si + 1, s->label);
        }
        lv_label_set_text(c->name, buf);

        snprintf(buf, sizeof buf, "%d%%", (filled_pm + 5) / 10);
        lv_label_set_text(c->pct, buf);
    }
}

static void housekeeping_cb(lv_timer_t *t) {
    (void)t;
    uint32_t now = lv_tick_get();

    int on = g.hb_seen && (now - g.hb_last) < HB_MS;
    lv_obj_set_style_bg_color(g.hb, on ? C_HB_ON : C_HB_OFF, 0);

    int phase = (now / TOGGLE_MS) % 2;
    int pages = page_count();
    int page = (pages > 1) ? (int)((now / PAGE_MS) % pages) : 0;
    if (phase != g.phase || page != g.page) {
        g.phase = phase;
        g.page = page;
        refresh();
    }
}

static void make_column(int i) {
    int x0 = MARGIN_X + i * (COL_W + COL_GAP);
    lv_obj_t *scr = lv_screen_active();

    lv_obj_t *col = lv_obj_create(scr);
    lv_obj_remove_style_all(col);
    lv_obj_set_size(col, COL_W, COL_H);
    lv_obj_set_pos(col, x0, TOP_Y);
    lv_obj_set_style_radius(col, 3, 0);
    lv_obj_set_style_clip_corner(col, true, 0);
    lv_obj_set_style_bg_opa(col, LV_OPA_COVER, 0);
    lv_obj_clear_flag(col, LV_OBJ_FLAG_SCROLLABLE);
    g.cols[i].col = col;

    for (int k = 0; k < MAX_SEGS; k++) {
        lv_obj_t *seg = lv_obj_create(col);
        lv_obj_remove_style_all(seg);
        lv_obj_set_style_bg_opa(seg, LV_OPA_COVER, 0);
        lv_obj_clear_flag(seg, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(seg, LV_OBJ_FLAG_HIDDEN);
        g.cols[i].seg[k] = seg;

        lv_obj_t *num = lv_label_create(scr);
        lv_obj_set_style_text_align(num, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(num, C_TEXT, 0);
        lv_obj_set_style_text_font(num, &lv_font_montserrat_12, 0);
        lv_obj_add_flag(num, LV_OBJ_FLAG_HIDDEN);
        g.cols[i].num[k] = num;
    }

    lv_obj_t *name = lv_label_create(scr);
    lv_obj_set_width(name, COL_W - 4);
    lv_obj_set_pos(name, x0 + 2, TOP_Y + 4);
    lv_label_set_long_mode(name, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_align(name, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(name, C_TEXT, 0);
    lv_obj_set_style_text_font(name, &lv_font_montserrat_12, 0);
    g.cols[i].name = name;

    lv_obj_t *pct = lv_label_create(scr);
    lv_obj_set_width(pct, COL_W - 4);
    lv_obj_set_pos(pct, x0 + 2, BOT_Y - 20);
    lv_obj_set_style_text_align(pct, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(pct, C_TEXT, 0);
    lv_obj_set_style_text_font(pct, &lv_font_montserrat_14, 0);
    g.cols[i].pct = pct;
}

void ui_create(void) {
    memset(&g, 0, sizeof g);
    model_init(&g.model);
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    for (int i = 0; i < COLS_PER_PAGE; i++) make_column(i);

    g.waiting = lv_label_create(scr);
    lv_label_set_text(g.waiting, "waiting for devices");
    lv_obj_set_style_text_color(g.waiting, lv_color_hex(0x9AA0A6), 0);
    lv_obj_center(g.waiting);

    g.hb = lv_obj_create(scr);
    lv_obj_remove_style_all(g.hb);
    lv_obj_set_size(g.hb, 6, 6);
    lv_obj_set_pos(g.hb, 2, 2);
    lv_obj_set_style_radius(g.hb, 2, 0);
    lv_obj_set_style_bg_opa(g.hb, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(g.hb, C_HB_OFF, 0);

    refresh();
    lv_timer_create(housekeeping_cb, 60, NULL);
}

void ui_update(const model_t *m) {
    g.model = *m;
    refresh();
}

void ui_heartbeat(void) {
    g.hb_seen = 1;
    g.hb_last = lv_tick_get();
    if (g.hb) lv_obj_set_style_bg_color(g.hb, C_HB_ON, 0);
}
