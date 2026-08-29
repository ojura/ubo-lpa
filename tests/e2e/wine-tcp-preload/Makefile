CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -fPIC -Wall -Wextra -Werror -Wformat=2
LDFLAGS ?=

.PHONY: all check clean

all: libwine-tcp-preload.so tests/bridge_test

libwine-tcp-preload.so: wine-tcp-preload.c
	$(CC) $(CFLAGS) -shared -o $@ $< $(LDFLAGS) -ldl -pthread

tests/bridge_test: tests/bridge_test.c
	$(CC) -std=gnu11 -O2 -g -Wall -Wextra -Werror -Wformat=2 -o $@ $<

check: all
	WINE_TCP_PRELOAD_TRANSLATE_ALL=1 WINE_TCP_PRELOAD_HANDSHAKE_TIMEOUT_MS=250 \
		LD_PRELOAD=$(CURDIR)/libwine-tcp-preload.so ./tests/bridge_test

clean:
	rm -f libwine-tcp-preload.so tests/bridge_test
