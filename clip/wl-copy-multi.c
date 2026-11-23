// wl-copy-multi: put one clipboard selection on Wayland that offers several MIME types at once,
// e.g. text/html for rich-text editors and text/plain for terminals. wl-copy can offer only one.
//
// Input on stdin: "mime\0content\0mime\0content\0...". Uses ext-data-control-v1, so no window or
// focus is needed (KWin, sway, Hyprland...). Stays running to serve pastes and exits when another
// program takes the clipboard. Build: see clip/Makefile.
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-client.h>
#include "ext-data-control-v1-client-protocol.h"

#define MAX_TYPES 16
static struct { char *mime; char *data; size_t len; } types[MAX_TYPES];
static int ntypes;
static struct wl_seat *seat;
static struct ext_data_control_manager_v1 *manager;
static int done;

static void registry_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
    if (!seat && strcmp(iface, wl_seat_interface.name) == 0)
        seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
    else if (strcmp(iface, ext_data_control_manager_v1_interface.name) == 0)
        manager = wl_registry_bind(r, name, &ext_data_control_manager_v1_interface, 1);
}
static void registry_remove(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener registry_listener = {registry_global, registry_remove};

static void source_send(void *d, struct ext_data_control_source_v1 *s, const char *mime, int32_t fd) {
    for (int i = 0; i < ntypes; i++) {
        if (strcmp(types[i].mime, mime) != 0) continue;
        size_t off = 0;
        while (off < types[i].len) {
            ssize_t n = write(fd, types[i].data + off, types[i].len - off);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) break;
            off += (size_t)n;
        }
        break;
    }
    close(fd);
}
static void source_cancelled(void *d, struct ext_data_control_source_v1 *s) { done = 1; }
static const struct ext_data_control_source_v1_listener source_listener = {source_send, source_cancelled};

// Offers for the current selection (including our own) arrive here; we never read them.
static void dev_offer(void *d, struct ext_data_control_device_v1 *dev, struct ext_data_control_offer_v1 *o) {}
static void dev_selection(void *d, struct ext_data_control_device_v1 *dev, struct ext_data_control_offer_v1 *o) {
    if (o) ext_data_control_offer_v1_destroy(o);
}
static void dev_finished(void *d, struct ext_data_control_device_v1 *dev) { done = 1; }
static void dev_primary(void *d, struct ext_data_control_device_v1 *dev, struct ext_data_control_offer_v1 *o) {
    if (o) ext_data_control_offer_v1_destroy(o);
}
static const struct ext_data_control_device_v1_listener device_listener = {dev_offer, dev_selection, dev_finished, dev_primary};

static char *read_all(size_t *len) {
    size_t cap = 65536, n = 0;
    char *buf = malloc(cap);
    for (;;) {
        if (n == cap) buf = realloc(buf, cap *= 2);
        ssize_t r = read(STDIN_FILENO, buf + n, cap - n);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) break;
        n += (size_t)r;
    }
    *len = n;
    return buf;
}

int main(void) {
    size_t len;
    char *in = read_all(&len), *p = in, *end = in + len;
    while (p < end && ntypes < MAX_TYPES) {           // parse mime\0content\0 pairs
        char *mime = p, *z = memchr(p, 0, (size_t)(end - p));
        if (!z) break;
        char *data = z + 1, *z2 = memchr(data, 0, (size_t)(end - data));
        size_t dlen = z2 ? (size_t)(z2 - data) : (size_t)(end - data);
        types[ntypes].mime = mime; types[ntypes].data = data; types[ntypes].len = dlen; ntypes++;
        p = data + dlen + 1;
    }
    if (ntypes == 0) { fprintf(stderr, "wl-copy-multi: nothing on stdin\n"); return 2; }

    struct wl_display *display = wl_display_connect(NULL);
    if (!display) { fprintf(stderr, "wl-copy-multi: no Wayland display\n"); return 1; }
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(display);
    if (!seat || !manager) { fprintf(stderr, "wl-copy-multi: compositor lacks ext-data-control-v1\n"); return 1; }

    struct ext_data_control_device_v1 *device = ext_data_control_manager_v1_get_data_device(manager, seat);
    ext_data_control_device_v1_add_listener(device, &device_listener, NULL);
    struct ext_data_control_source_v1 *source = ext_data_control_manager_v1_create_data_source(manager);
    ext_data_control_source_v1_add_listener(source, &source_listener, NULL);
    for (int i = 0; i < ntypes; i++) ext_data_control_source_v1_offer(source, types[i].mime);
    ext_data_control_device_v1_set_selection(device, source);
    wl_display_flush(display);

    signal(SIGPIPE, SIG_IGN);
    while (!done && wl_display_dispatch(display) != -1) {}
    ext_data_control_source_v1_destroy(source);
    wl_display_disconnect(display);
    return 0;
}
