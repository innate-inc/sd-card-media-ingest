#include "ui.h"

#include "lvgl.h"

#include <stdio.h>
#include <string.h>

#define COLS_PER_PAGE 4
#define PAGE_MS   4000   /* cycle pages when there are more than 4 devices */
#define TOGGLE_MS 2000   /* 0.5 Hz: name <-> eta/size */
#define STALE_MS  2000   /* no host message this long -> "no signal" scrim */
#define LONG_MS    600   /* press >= this = "long press" (navigation) */
#define ARM_MS    5000   /* deliberate hold-to-arm-the-wipe in the detail screen */
#define IDLE_MS  12000   /* no input this long -> leave select/detail */

#define MARGIN_X 6
#define TOP_Y    12
#define BOT_Y    (UI_H - 6)
#define COL_GAP  4
#define COL_H    (BOT_Y - TOP_Y)
#define COL_W    ((UI_W - 2 * MARGIN_X - (COLS_PER_PAGE - 1) * COL_GAP) / COLS_PER_PAGE)

#define C_TEXT   lv_color_hex(0xFFFFFF)

typedef struct {
    lv_obj_t *col;                 /* background = empty colour */
    lv_obj_t *seg[MAX_SEGS];       /* stacked segment rects */
    lv_obj_t *num[MAX_SEGS];       /* per-segment numbers (optional) */
    lv_obj_t *name;
} column_t;

typedef struct {
    lv_obj_t *sw;                  /* colour swatch */
    lv_obj_t *txt;                 /* what it means */
} legrow_t;

/* One-button navigation states. */
typedef enum {
    NAV_BROWSE = 0,  /* auto-cycling status view */
    NAV_SELECT,      /* a card (or the legend stop) is highlighted; short=next,
                        long=open card */
    NAV_DETAIL,      /* card detail; short=back, hold 5s=wipe */
} nav_t;

static struct {
    model_t model;
    column_t cols[COLS_PER_PAGE];
    legrow_t leg[MAX_LEGEND];
    lv_obj_t *legtitle;
    lv_obj_t *waiting;
    lv_obj_t *stale;               /* dim grey full-screen overlay when the feed goes quiet */
    lv_obj_t *selbox;              /* white highlight around the selected card */
    lv_obj_t *det;                 /* detail-screen container */
    lv_obj_t *det_title;
    lv_obj_t *det_body;
    lv_obj_t *det_btn;             /* red delete zone (right quarter) */
    lv_obj_t *det_fill;            /* rises from the bottom while holding to arm */
    lv_obj_t *det_btn_lbl;
    uint32_t hb_last;
    int hb_seen;
    int phase;
    int page;
    nav_t nav;
    int sel;                       /* selected slot index (0..count-1) */
    uint32_t last_input;
    uint32_t press_start;
    int pressing;
    int fired;                     /* long-press already fired this hold */
    ui_confirm_cb_t confirm_cb;
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

static void fmt_mbps(char *buf, size_t n, int kbps) {
    if (kbps < 0) { buf[0] = '\0'; return; }
    double mb = kbps / 1000.0;                 /* KB/s -> MB/s (decimal) */
    if (mb >= 10) snprintf(buf, n, "%.0fMB/s", mb);
    else snprintf(buf, n, "%.1fMB/s", mb);
}

/* The legend, when the server sends one, is the leftmost page (page 0);
 * card pages follow it. */
static int has_legend(void) { return g.model.nlegend > 0; }

static int card_pages(void) {
    int total = g.model.count;
    return (total <= 0) ? 0 : (total + COLS_PER_PAGE - 1) / COLS_PER_PAGE;
}

static int page_count(void) {
    int pages = card_pages() + (has_legend() ? 1 : 0);
    return pages < 1 ? 1 : pages;
}

static void hide_cards(void) {
    for (int i = 0; i < COLS_PER_PAGE; i++) {
        lv_obj_add_flag(g.cols[i].col, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(g.cols[i].name, LV_OBJ_FLAG_HIDDEN);
        for (int k = 0; k < MAX_SEGS; k++)
            lv_obj_add_flag(g.cols[i].num[k], LV_OBJ_FLAG_HIDDEN);
    }
}

static void hide_legend(void) {
    lv_obj_add_flag(g.legtitle, LV_OBJ_FLAG_HIDDEN);
    for (int k = 0; k < MAX_LEGEND; k++) {
        lv_obj_add_flag(g.leg[k].sw, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(g.leg[k].txt, LV_OBJ_FLAG_HIDDEN);
    }
}

static int page_of_card(int ci) {
    return (has_legend() ? 1 : 0) + ci / COLS_PER_PAGE;
}

static void hide_selbox(void) { lv_obj_add_flag(g.selbox, LV_OBJ_FLAG_HIDDEN); }
static void hide_detail(void) { lv_obj_add_flag(g.det, LV_OBJ_FLAG_HIDDEN); }

/* A wipe is only offered for a card that has finished and is waiting to be
 * removed: `done` (safe to remove) or `pending` (verified, awaiting confirm).
 * The whole wipe flow is gated on this, so an empty slot, a still-copying
 * card, or an errored card can never be armed/confirmed for deletion. */
static int slot_wipeable(const slot_t *s) {
    return s->status == ST_DONE || s->status == ST_PENDING;
}

/* Name a segment by matching its colour to a legend entry (the legend and the
 * segment stack are independent lists, so index alignment isn't guaranteed). */
static const char *legend_for_color(uint32_t color) {
    for (int k = 0; k < g.model.nlegend; k++)
        if (g.model.legend[k].color == color) return g.model.legend[k].text;
    return "segment";
}

static const char *status_text(slot_status_t st) {
    switch (st) {
        case ST_ACTIVE:  return "copying";
        case ST_DONE:    return "done - safe to remove";
        case ST_ERROR:   return "ERROR";
        case ST_PAUSED:  return "paused";
        case ST_PENDING: return "verified - awaiting wipe";
        default:         return "idle";
    }
}

static void draw_detail(void) {
    slot_t *s = &g.model.slots[g.sel];
    lv_obj_clear_flag(g.det, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(g.det);

    char t[MAX_LABEL + 8];
    snprintf(t, sizeof t, "%d  %s", g.sel + 1, s->label);
    lv_label_set_text(g.det_title, t);

    char body[256];
    int o = 0;
    /* snprintf returns the *intended* length; clamp so `sizeof body - o` can
     * never go negative and wrap. */
    #define BODY_CLAMP() do { if (o > (int)sizeof body - 1) o = sizeof body - 1; } while (0)
    /* status first, so if the body ever overflows it's the least-important
     * bottom lines that get clipped, never the status. */
    o += snprintf(body + o, sizeof body - o, "%s\n", status_text(s->status));
    BODY_CLAMP();
    if (s->detail[0]) {
        o += snprintf(body + o, sizeof body - o, "%s\n", s->detail);
        BODY_CLAMP();
    }
    if (s->size_mb >= 0) {
        char gb[16];
        fmt_gb(gb, sizeof gb, s->size_mb / 1000.0);
        o += snprintf(body + o, sizeof body - o, "total %s\n", gb);
        BODY_CLAMP();
    }
    {   /* speed + eta share one line to save vertical space */
        char spd[16] = "", eta[16] = "";
        if (s->kbps >= 0) fmt_mbps(spd, sizeof spd, s->kbps);
        if (s->eta_s >= 0) fmt_eta(eta, sizeof eta, s->eta_s);
        if (spd[0] || eta[0]) {
            o += snprintf(body + o, sizeof body - o, "%s%s%s\n", spd,
                          (spd[0] && eta[0]) ? "  eta " : (eta[0] ? "eta " : ""),
                          eta);
            BODY_CLAMP();
        }
    }
    int filled = 0;
    for (int k = 0; k < MAX_SEGS && o < (int)sizeof body - 1; k++) {
        int pm = s->segs[k].permille;
        if (pm <= 0) continue;
        filled += pm;
        const char *nm = legend_for_color(s->segs[k].color);
        if (s->size_mb >= 0) {
            char gb[16];
            fmt_gb(gb, sizeof gb, s->size_mb / 1000.0 * pm / 1000.0);
            o += snprintf(body + o, sizeof body - o, "%s %s\n", nm, gb);
            BODY_CLAMP();
        }
    }
    if (s->size_mb >= 0 && filled < 1000) {
        char gb[16];
        fmt_gb(gb, sizeof gb, s->size_mb / 1000.0 * (1000 - filled) / 1000.0);
        o += snprintf(body + o, sizeof body - o, "free %s\n", gb);
        BODY_CLAMP();
    }
    lv_label_set_text(g.det_body, body);
    #undef BODY_CLAMP

    /* the delete zone is always present, but it is only armed (dark red,
     * "HOLD TO WIPE") once the card is wipeable; before that (still copying /
     * verifying) it is greyed out and holding it does nothing. */
    if (slot_wipeable(s)) {
        lv_obj_set_style_bg_color(g.det_btn, lv_color_hex(0x7A1A1A), 0);
        lv_label_set_text(g.det_btn_lbl, "HOLD\nTO\nWIPE");
    } else {
        lv_obj_set_style_bg_color(g.det_btn, lv_color_hex(0x333333), 0);
        lv_label_set_text(g.det_btn_lbl, "WIPE\nWHEN\nDONE");
        lv_obj_add_flag(g.det_fill, LV_OBJ_FLAG_HIDDEN);
    }
}

static void draw_legend(void) {
    hide_cards();
    lv_obj_clear_flag(g.legtitle, LV_OBJ_FLAG_HIDDEN);
    int rowH = 22;
    int y0 = TOP_Y + 24;
    for (int k = 0; k < MAX_LEGEND; k++) {
        if (k >= g.model.nlegend) {
            lv_obj_add_flag(g.leg[k].sw, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(g.leg[k].txt, LV_OBJ_FLAG_HIDDEN);
            continue;
        }
        int y = y0 + k * rowH;
        lv_obj_set_pos(g.leg[k].sw, MARGIN_X + 24, y + 2);
        lv_obj_set_style_bg_color(g.leg[k].sw,
                                  lv_color_hex(g.model.legend[k].color), 0);
        lv_obj_clear_flag(g.leg[k].sw, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(g.leg[k].txt, g.model.legend[k].text);
        lv_obj_set_pos(g.leg[k].txt, MARGIN_X + 24 + 22, y);
        lv_obj_clear_flag(g.leg[k].txt, LV_OBJ_FLAG_HIDDEN);
    }
}

static void refresh(void) {
    int total = g.model.count;

    /* a card can vanish (unplugged) while we're navigating it: keep sel valid.
     * In SELECT, sel == total is the legend stop (one past the last card). */
    if (g.nav != NAV_BROWSE) {
        if (total <= 0) g.nav = NAV_BROWSE;
        else {
            int maxsel = total - 1;
            if (g.nav == NAV_SELECT && has_legend()) maxsel = total;
            if (g.sel > maxsel) g.sel = maxsel;
        }
    }

    /* detail: full-screen card info, nothing else */
    if (g.nav == NAV_DETAIL && total > 0) {
        lv_obj_add_flag(g.waiting, LV_OBJ_FLAG_HIDDEN);
        hide_cards();
        hide_legend();
        hide_selbox();
        draw_detail();
        return;
    }
    hide_detail();

    if (total <= 0 && !has_legend()) {
        lv_obj_clear_flag(g.waiting, LV_OBJ_FLAG_HIDDEN);
        hide_cards();
        hide_legend();
        hide_selbox();
        return;
    }
    lv_obj_add_flag(g.waiting, LV_OBJ_FLAG_HIDDEN);

    /* in SELECT the page follows the highlighted card, not the auto-cycle;
     * the legend stop (sel == total) shows the legend page (page 0). */
    if (g.nav == NAV_SELECT && total > 0)
        g.page = (has_legend() && g.sel == total) ? 0 : page_of_card(g.sel);
    if (g.page >= page_count()) g.page = 0;

    if (has_legend() && g.page == 0) {
        draw_legend();
        hide_selbox();
        return;
    }
    hide_legend();

    int card_page = has_legend() ? g.page - 1 : g.page;
    int base = card_page * COLS_PER_PAGE;

    for (int i = 0; i < COLS_PER_PAGE; i++) {
        column_t *c = &g.cols[i];
        int si = base + i;
        int shown = si < total;
        lv_obj_t *toggle[] = {c->col, c->name};
        for (unsigned k = 0; k < 2; k++) {
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
        int accH = 0;
        for (int k = 0; k < MAX_SEGS; k++) {
            int pm = s->segs[k].permille;
            int h = (COL_H * pm) / 1000;
            /* segments are clamped individually but their sum isn't, so bound
             * the stack to the column so an oversized total can't overflow it */
            if (h > COL_H - accH) h = COL_H - accH;
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

            /* numbers are gigabytes only (no percentages) */
            if (g.model.show_numbers && h >= 12 && s->size_mb >= 0) {
                char nb[16];
                fmt_gb(nb, sizeof nb, s->size_mb / 1000.0 * pm / 1000.0);
                lv_label_set_text(c->num[k], nb);
                lv_obj_set_width(c->num[k], COL_W);
                lv_obj_set_pos(c->num[k], x0, TOP_Y + ytop + h / 2 - 7);
                lv_obj_clear_flag(c->num[k], LV_OBJ_FLAG_HIDDEN);
            } else {
                lv_obj_add_flag(c->num[k], LV_OBJ_FLAG_HIDDEN);
            }
            accH += h;
        }

        char buf[48];
        if (g.phase == 1) {
            char eta[16], size[16], spd[16];
            fmt_eta(eta, sizeof eta, s->eta_s);
            fmt_mbps(spd, sizeof spd, s->kbps);
            if (s->size_mb >= 0) fmt_gb(size, sizeof size, s->size_mb / 1000.0);
            else size[0] = '\0';
            if (s->status == ST_DONE) snprintf(buf, sizeof buf, "done");
            /* while copying, show ETA + speed; otherwise fall back to size */
            else if (eta[0] && spd[0]) snprintf(buf, sizeof buf, "%s %s", eta, spd);
            else if (eta[0] && size[0]) snprintf(buf, sizeof buf, "%s %s", eta, size);
            else if (eta[0] || size[0]) snprintf(buf, sizeof buf, "%s%s", eta, size);
            else snprintf(buf, sizeof buf, "%d %s", si + 1, s->label);
        } else {
            snprintf(buf, sizeof buf, "%d %s", si + 1, s->label);
        }
        /* a done card is a full green bar -> black text reads on it */
        lv_obj_set_style_text_color(
            c->name, s->status == ST_DONE ? lv_color_black() : C_TEXT, 0);
        lv_label_set_text(c->name, buf);
    }

    /* white highlight around the selected card while browsing with the button */
    if (g.nav == NAV_SELECT && total > 0) {
        int i = g.sel % COLS_PER_PAGE;
        int x0 = MARGIN_X + i * (COL_W + COL_GAP);
        lv_obj_set_pos(g.selbox, x0 - 2, TOP_Y - 2);
        lv_obj_set_size(g.selbox, COL_W + 4, COL_H + 4);
        lv_obj_clear_flag(g.selbox, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(g.selbox);
    } else {
        hide_selbox();
    }
}

/* True once the host has spoken but then gone quiet: data is stale, so the UI
 * freezes and the button is dead until the feed returns. */
static int feed_stale(void) {
    return g.hb_seen && (lv_tick_get() - g.hb_last) > STALE_MS;
}

/* While the button is held in the detail screen, the delete zone fills like a
 * progress bar toward the arm threshold. */
static void update_arm_fill(uint32_t now) {
    if (g.det_fill == NULL) return;
    if (g.nav == NAV_DETAIL && g.pressing && !g.fired
        && slot_wipeable(&g.model.slots[g.sel])) {
        uint32_t held = now - g.press_start;
        if (held > ARM_MS) held = ARM_MS;
        int h = (int)((uint64_t)UI_H * held / ARM_MS);
        if (h > 0) {
            lv_obj_set_size(g.det_fill, UI_W / 4, h);
            lv_obj_set_pos(g.det_fill, 0, UI_H - h);
            lv_obj_clear_flag(g.det_fill, LV_OBJ_FLAG_HIDDEN);
            return;
        }
    }
    lv_obj_add_flag(g.det_fill, LV_OBJ_FLAG_HIDDEN);
}

static void housekeeping_cb(lv_timer_t *t) {
    (void)t;
    uint32_t now = lv_tick_get();

    /* feed liveness: if the host has gone quiet, drop the dim grey scrim on top
     * and make the UI inert -- abort any in-progress navigation/hold so a
     * stale screen can't be used to arm a wipe. */
    if (feed_stale()) {
        if (g.nav != NAV_BROWSE || g.pressing) {
            g.nav = NAV_BROWSE;
            g.pressing = 0;
            g.fired = 0;
            update_arm_fill(now);
            refresh();
        }
        lv_obj_clear_flag(g.stale, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(g.stale);
    } else {
        lv_obj_add_flag(g.stale, LV_OBJ_FLAG_HIDDEN);
    }

    /* a long press fires the moment the hold threshold is reached (not on
     * release), so the arm fill can grow up to it and act on completion.
     * Arming the wipe demands a much longer, deliberate hold than navigation. */
    /* a wipeable card in detail demands the long, deliberate ARM_MS hold; any
     * other press (navigation, or a view-only detail) uses the shorter LONG_MS */
    uint32_t thr = (g.nav == NAV_DETAIL
                    && slot_wipeable(&g.model.slots[g.sel])) ? ARM_MS : LONG_MS;
    if (g.pressing && !g.fired && (now - g.press_start) >= thr) {
        g.fired = 1;
        ui_button(UI_BTN_LONG);
    }
    update_arm_fill(now);

    /* fall back to the auto-cycling status view after a spell of no input */
    if (g.nav != NAV_BROWSE && (now - g.last_input) > IDLE_MS) {
        g.nav = NAV_BROWSE;
        refresh();
    }

    /* The name<->size label toggle keeps ticking in BROWSE and SELECT -- a
     * click only pauses page auto-advance, not the label cycle. DETAIL stays
     * static so the hold-to-arm gesture isn't disturbed. */
    if (g.nav == NAV_DETAIL) return;

    int phase = (now / TOGGLE_MS) % 2;
    int page = g.page;
    if (g.nav == NAV_BROWSE) {          /* pages auto-advance only when browsing */
        int pages = page_count();
        page = (pages > 1) ? (int)((now / PAGE_MS) % pages) : 0;
    }
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
    lv_label_set_long_mode(name, LV_LABEL_LONG_WRAP);   /* wrap, don't ellipsize */
    lv_obj_set_style_text_align(name, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(name, C_TEXT, 0);
    lv_obj_set_style_text_font(name, &lv_font_montserrat_12, 0);
    g.cols[i].name = name;
}

static void make_legend(void) {
    lv_obj_t *scr = lv_screen_active();

    g.legtitle = lv_label_create(scr);
    lv_label_set_text(g.legtitle, "Legend");
    lv_obj_set_pos(g.legtitle, MARGIN_X + 24, TOP_Y);
    lv_obj_set_style_text_color(g.legtitle, C_TEXT, 0);
    lv_obj_set_style_text_font(g.legtitle, &lv_font_montserrat_16, 0);
    lv_obj_add_flag(g.legtitle, LV_OBJ_FLAG_HIDDEN);

    for (int k = 0; k < MAX_LEGEND; k++) {
        lv_obj_t *sw = lv_obj_create(scr);
        lv_obj_remove_style_all(sw);
        lv_obj_set_size(sw, 16, 16);
        lv_obj_set_style_radius(sw, 3, 0);
        lv_obj_set_style_bg_opa(sw, LV_OPA_COVER, 0);
        lv_obj_clear_flag(sw, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(sw, LV_OBJ_FLAG_HIDDEN);
        g.leg[k].sw = sw;

        lv_obj_t *txt = lv_label_create(scr);
        lv_obj_set_style_text_color(txt, C_TEXT, 0);
        lv_obj_set_style_text_font(txt, &lv_font_montserrat_14, 0);
        lv_obj_add_flag(txt, LV_OBJ_FLAG_HIDDEN);
        g.leg[k].txt = txt;
    }
}

static void make_selbox(void) {
    lv_obj_t *scr = lv_screen_active();
    lv_obj_t *b = lv_obj_create(scr);
    lv_obj_remove_style_all(b);
    lv_obj_set_style_bg_opa(b, LV_OPA_TRANSP, 0);
    lv_obj_set_style_radius(b, 4, 0);
    lv_obj_set_style_border_color(b, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_border_width(b, 3, 0);
    lv_obj_set_style_border_opa(b, LV_OPA_COVER, 0);
    lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(b, LV_OBJ_FLAG_HIDDEN);
    g.selbox = b;
}

static void make_detail(void) {
    lv_obj_t *scr = lv_screen_active();

    lv_obj_t *d = lv_obj_create(scr);
    lv_obj_remove_style_all(d);
    lv_obj_set_size(d, UI_W, UI_H);
    lv_obj_set_pos(d, 0, 0);
    lv_obj_set_style_bg_color(d, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    lv_obj_clear_flag(d, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(d, LV_OBJ_FLAG_HIDDEN);
    g.det = d;

    g.det_title = lv_label_create(d);
    lv_obj_set_pos(g.det_title, 8, 6);
    lv_obj_set_style_text_color(g.det_title, C_TEXT, 0);
    lv_obj_set_style_text_font(g.det_title, &lv_font_montserrat_16, 0);

    g.det_body = lv_label_create(d);
    lv_obj_set_pos(g.det_body, 8, 30);
    lv_obj_set_width(g.det_body, UI_W * 3 / 4 - 16);
    lv_obj_set_style_text_color(g.det_body, C_TEXT, 0);
    lv_obj_set_style_text_font(g.det_body, &lv_font_montserrat_12, 0);

    lv_obj_t *btn = lv_obj_create(d);
    lv_obj_remove_style_all(btn);
    lv_obj_set_size(btn, UI_W / 4, UI_H);
    lv_obj_set_pos(btn, UI_W * 3 / 4, 0);
    lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, 0);
    lv_obj_clear_flag(btn, LV_OBJ_FLAG_SCROLLABLE);
    g.det_btn = btn;

    /* rises from the bottom of the delete zone as you hold to arm */
    lv_obj_t *fill = lv_obj_create(btn);
    lv_obj_remove_style_all(fill);
    lv_obj_set_style_bg_color(fill, lv_color_hex(0xE63946), 0);
    lv_obj_set_style_bg_opa(fill, LV_OPA_COVER, 0);
    lv_obj_clear_flag(fill, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(fill, LV_OBJ_FLAG_HIDDEN);
    g.det_fill = fill;

    g.det_btn_lbl = lv_label_create(btn);
    lv_obj_center(g.det_btn_lbl);
    lv_obj_set_style_text_align(g.det_btn_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(g.det_btn_lbl, C_TEXT, 0);
    lv_obj_set_style_text_font(g.det_btn_lbl, &lv_font_montserrat_16, 0);
}

void ui_create(void) {
    memset(&g, 0, sizeof g);
    model_init(&g.model);
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    for (int i = 0; i < COLS_PER_PAGE; i++) make_column(i);
    make_legend();
    make_selbox();
    make_detail();

    g.waiting = lv_label_create(scr);
    lv_label_set_text(g.waiting, "waiting for devices");
    lv_obj_set_style_text_color(g.waiting, lv_color_hex(0x9AA0A6), 0);
    lv_obj_center(g.waiting);

    /* feed-lost overlay: a dim grey scrim over everything, with the last frame
     * still faintly visible underneath. Grey (not red) so red stays reserved
     * for real errors; this is just "the feed went quiet". */
    g.stale = lv_obj_create(scr);
    lv_obj_remove_style_all(g.stale);
    lv_obj_set_size(g.stale, UI_W, UI_H);
    lv_obj_set_pos(g.stale, 0, 0);
    lv_obj_set_style_bg_color(g.stale, lv_color_hex(0x141414), 0);
    lv_obj_set_style_bg_opa(g.stale, LV_OPA_70, 0);
    lv_obj_clear_flag(g.stale, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(g.stale, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *stale_lbl = lv_label_create(g.stale);
    lv_label_set_text(stale_lbl, "no signal");
    lv_obj_set_style_text_color(stale_lbl, lv_color_hex(0x9AA0A6), 0);
    lv_obj_center(stale_lbl);

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
    if (g.stale) lv_obj_add_flag(g.stale, LV_OBJ_FLAG_HIDDEN);
}

void ui_set_confirm_cb(ui_confirm_cb_t cb) { g.confirm_cb = cb; }

/* One button, four states. See the nav_t comments; short vs long is the only
 * distinction, so every screen means something different by the same two
 * gestures. */
void ui_button(int kind) {
    if (feed_stale()) return;           /* dead feed -> dead button */
    g.last_input = lv_tick_get();
    int total = g.model.count;

    switch (g.nav) {
    case NAV_BROWSE:
        if (total > 0) { g.nav = NAV_SELECT; g.sel = 0; }
        break;
    case NAV_SELECT:
        if (total <= 0) { g.nav = NAV_BROWSE; break; }
        /* long-press opens the detail view of any card (the legend stop, sel ==
         * total, is not a card). Detail is view-only until the card is
         * wipeable -- the delete zone only appears then (see draw_detail). */
        if (kind == UI_BTN_LONG) {
            if (g.sel < total) g.nav = NAV_DETAIL;
        } else {
            /* short = next card, then the legend page (if any), then wrap */
            int stops = total + (has_legend() ? 1 : 0);
            g.sel = (g.sel + 1) % stops;
        }
        break;
    case NAV_DETAIL:
        if (kind == UI_BTN_LONG) {                    /* 5s hold completed */
            /* re-check: state may have changed while we were in detail */
            if (g.confirm_cb && slot_wipeable(&g.model.slots[g.sel]))
                g.confirm_cb(g.sel);                  /* fire the wipe */
            g.nav = NAV_SELECT;
        } else {
            g.nav = NAV_SELECT;                       /* short = back */
        }
        break;
    }
    refresh();
}

void ui_button_down(void) {
    if (feed_stale()) return;           /* dead feed -> dead button */
    g.pressing = 1;
    g.fired = 0;
    g.press_start = lv_tick_get();
    g.last_input = g.press_start;
}

void ui_button_up(void) {
    if (!g.pressing) return;
    g.pressing = 0;
    /* long press already fired at the threshold (see housekeeping_cb); a
     * release before then is a short press. */
    if (!g.fired) ui_button(UI_BTN_SHORT);
    update_arm_fill(lv_tick_get());   /* clear the fill immediately on release */
}
