# Builds wl-copy-multi. Needs gcc, wayland-scanner and libwayland-client.
# Protocol: ext-data-control-v1 (KWin 6.8+). The official XML ships in `wayland-protocols`; if that
# isn't installed, it is derived from the wlr-data-control XML (same messages, renamed interfaces),
# which qt6-wayland ships.
EXT ?= $(wildcard /usr/share/wayland-protocols/staging/ext-data-control/ext-data-control-v1.xml)
WLR ?= $(firstword $(wildcard /usr/share/wlr-protocols/unstable/wlr-data-control-unstable-v1.xml \
                             /usr/share/qt6/wayland/protocols/wlr-data-control/wlr-data-control-unstable-v1.xml))
GEN = ext-data-control-v1

wl-copy-multi: wl-copy-multi.c $(GEN)-client-protocol.h $(GEN)-protocol.c
	$(CC) -O2 -Wall -o $@ wl-copy-multi.c $(GEN)-protocol.c $$(pkg-config --cflags --libs wayland-client)

$(GEN).xml:
ifneq ($(EXT),)
	cp $(EXT) $@
else
	sed -e 's/zwlr_data_control/ext_data_control/g' -e 's/ version="2"/ version="1"/' -e 's/ since="2"//' $(WLR) > $@
endif

$(GEN)-client-protocol.h: $(GEN).xml
	wayland-scanner client-header $< $@

$(GEN)-protocol.c: $(GEN).xml
	wayland-scanner private-code $< $@

clean:
	rm -f wl-copy-multi $(GEN).xml $(GEN)-client-protocol.h $(GEN)-protocol.c
