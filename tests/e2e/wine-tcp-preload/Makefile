CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -fPIC -Wall -Wextra -Werror -Wformat=2
LDFLAGS ?=

MULTIARCH64 := $(shell $(CC) -print-multiarch)
MULTIARCH32 := $(shell $(CC) -m32 -print-multiarch)
LOADER32 := $(shell $(CC) -m32 -print-file-name=ld-linux.so.2)
SHIM64 := preload/lib/$(MULTIARCH64)/libwine-tcp-preload.so
SHIM32 := preload/lib/$(MULTIARCH32)/libwine-tcp-preload.so

.PHONY: all preload check clean

all: preload tests/bridge_test tests/bridge_test32

preload: $(SHIM64) $(SHIM32)

$(SHIM64): wine-tcp-preload.c
	mkdir -p "$(@D)"
	$(CC) $(CFLAGS) -shared -o $@ $< $(LDFLAGS) -ldl -pthread

$(SHIM32): wine-tcp-preload.c
	mkdir -p "$(@D)"
	$(CC) -m32 $(CFLAGS) -shared -o $@ $< $(LDFLAGS) -ldl -pthread

tests/bridge_test: tests/bridge_test.c
	$(CC) -std=gnu11 -O2 -g -Wall -Wextra -Werror -Wformat=2 -o $@ $<

tests/bridge_test32: tests/bridge_test.c
	$(CC) -m32 -std=gnu11 -O2 -g -Wall -Wextra -Werror -Wformat=2 -o $@ $<

check: all
	WINE_TCP_PRELOAD_TRANSLATE_ALL=1 WINE_TCP_PRELOAD_HANDSHAKE_TIMEOUT_MS=250 \
		LD_PRELOAD='$(CURDIR)/preload/$$LIB/libwine-tcp-preload.so' \
		./tests/bridge_test
	@set +e; "$(LOADER32)" --verify ./tests/bridge_test32 >/dev/null 2>&1; \
	status=$$?; set -e; \
	if [ "$$status" -eq 0 ]; then \
		WINE_TCP_PRELOAD_TRANSLATE_ALL=1 WINE_TCP_PRELOAD_HANDSHAKE_TIMEOUT_MS=250 \
			LD_PRELOAD='$(CURDIR)/preload/$$LIB/libwine-tcp-preload.so' \
			./tests/bridge_test32; \
	else \
		printf '%s\n' 'bridge test (32-bit): SKIP (kernel cannot execute i386 ELF)'; \
	fi

clean:
	rm -f "$(SHIM64)" "$(SHIM32)" tests/bridge_test tests/bridge_test32
	rmdir "preload/lib/$(MULTIARCH64)" "preload/lib/$(MULTIARCH32)" \
		preload/lib preload 2>/dev/null || true
