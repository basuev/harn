.DEFAULT_GOAL := all
PREFIX ?= /usr/local
CC ?= cc
STRIP ?= strip
CONFIG ?= config.h
BUILD ?= .build
BIN ?= harn
EXTRA_SOURCES ?=
PROVIDER_DEPS =
PROVIDER_LICENSES =
-include $(wildcard providers/*/build.mk)
RELEASE = harn-$(shell sed -n 's/^#define VERSION "\(.*\)"/\1/p' config.def.h)-$(shell uname -s | tr A-Z a-z)-$(shell uname -m)
REVISION := $(shell cat .core-revision 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo source)
CPPFLAGS += -I$(CURDIR) -DHARN_CONFIG='"$(CONFIG)"' -DHARN_REVISION='"$(REVISION)"'
CFLAGS ?= -O2
CFLAGS += -std=c11 -Wall -Wextra
CPPFLAGS += -ffile-prefix-map="$(CURDIR)=." -D_XOPEN_SOURCE=700 $(shell pkg-config --cflags libcurl jansson yaml-0.1 md4c)
LDLIBS += $(shell pkg-config --libs libcurl jansson yaml-0.1 md4c)
SOURCES = main.c util.c session.c jobs.c tools.c instructions.c skills.c connections.c provider.c cache.c auth.c ui.c render.c vendor/linenoise/linenoise.c $(wildcard providers/*/*.c) $(EXTRA_SOURCES)
OBJECTS = $(SOURCES:%.c=$(BUILD)/%.o)

all: $(BIN)

config.h:
	cp config.def.h $@

$(OBJECTS): harn.h $(CONFIG) config.def.h vendor/linenoise/linenoise.h $(wildcard providers/*/*.h) $(PROVIDER_DEPS)
$(BUILD)/connections.o: FORCE
$(BUILD)/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(BIN): $(OBJECTS) $(PROVIDER_DEPS)
	$(CC) $(LDFLAGS) -o $@ $(OBJECTS) $(LDLIBS)

fixture:
	$(MAKE) CONFIG=tests/config.h BUILD=.build/test BIN=.build/harn-test EXTRA_SOURCES=tests/fixture.c

check-core: fixture
	python3 tests/check.py
	python3 tests/responses.py
	python3 tests/cache.py
	python3 tests/cache_benchmark.py
	python3 tests/ui.py
	python3 tests/render.py
	python3 tests/job_control.py

check: check-core
	@set -e; for check in $(wildcard providers/*/check.py); do python3 "$$check"; done

check-streaming: fixture
	python3 tests/streaming.py

check-terminal: fixture
	python3 tests/terminal.py

check-patches:
	python3 tests/patches.py

check-release: release
	python3 tests/release.py dist/$(RELEASE).tar.gz

install: $(BIN)
	install -d $(DESTDIR)$(PREFIX)/bin $(DESTDIR)$(PREFIX)/share/licenses/harn
	install -m 755 $(BIN) $(DESTDIR)$(PREFIX)/bin/harn
	install -m 644 LICENSE $(DESTDIR)$(PREFIX)/share/licenses/harn/LICENSE
	install -m 644 vendor/linenoise/LICENSE $(DESTDIR)$(PREFIX)/share/licenses/harn/LICENSE.linenoise
	@set -e; for entry in $(PROVIDER_LICENSES); do name=$${entry%%:*}; file=$${entry#*:}; install -m 644 "$$file" "$(DESTDIR)$(PREFIX)/share/licenses/harn/LICENSE.$$name"; done

# Export committed sources so applied patches and personal configuration cannot leak.
release:
	rm -rf .build/release-source dist/$(RELEASE)
	mkdir -p .build/release-source dist/$(RELEASE)/bin dist/$(RELEASE)/licenses dist/$(RELEASE)/source
	git archive HEAD | tar -x -C .build/release-source
	$(MAKE) -C .build/release-source CONFIG=config.def.h REVISION=$$(git rev-parse HEAD)
	install -m 755 .build/release-source/harn dist/$(RELEASE)/bin/harn
	$(STRIP) dist/$(RELEASE)/bin/harn
	cp .build/release-source/LICENSE dist/$(RELEASE)/licenses/LICENSE
	cp .build/release-source/vendor/linenoise/LICENSE dist/$(RELEASE)/licenses/LICENSE.linenoise
	git archive HEAD | tar -x -C dist/$(RELEASE)/source
	git rev-parse HEAD > dist/$(RELEASE)/source/.core-revision
	tar -C dist -czf dist/$(RELEASE).tar.gz $(RELEASE)

clean:
	rm -rf .build harn

FORCE:
.PHONY: all fixture check check-core check-patches check-streaming check-terminal check-release install release clean FORCE
