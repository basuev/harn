PREFIX ?= /usr/local
CC ?= cc
STRIP ?= strip
CONFIG ?= config.h
BUILD ?= .build
BIN ?= harn
RELEASE = harn-$(shell sed -n 's/^#define VERSION "\(.*\)"/\1/p' config.def.h)-$(shell uname -s | tr A-Z a-z)-$(shell uname -m)
CPPFLAGS += -I$(CURDIR) -DHARN_CONFIG='"$(CONFIG)"'
CFLAGS ?= -O2
CFLAGS += -std=c11 -Wall -Wextra
CPPFLAGS += -ffile-prefix-map="$(CURDIR)=." -D_XOPEN_SOURCE=700 -Ivendor/libjwt/include -I.build/jwt $(shell pkg-config --cflags libcurl jansson openssl yaml-0.1 md4c)
LDLIBS += .build/jwt/libjwt.a $(shell pkg-config --libs libcurl jansson openssl yaml-0.1 md4c)
SOURCES = main.c util.c session.c jobs.c tools.c instructions.c skills.c connections.c provider.c auth.c ui.c render.c vendor/linenoise/linenoise.c
OBJECTS = $(SOURCES:%.c=$(BUILD)/%.o)

all: $(BIN)

config.h:
	cp config.def.h $@

.build/jwt/libjwt.a:
	cmake -S vendor/libjwt -B .build/jwt -DWITH_TESTS=OFF -DWITH_GNUTLS=OFF -DWITH_MBEDTLS=OFF -DWITH_OPENSSL=ON -DWITH_LIBCURL=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS:STRING='-ffile-prefix-map="$(CURDIR)=."'
	cmake --build .build/jwt --target jwt_static --parallel

$(OBJECTS): harn.h $(CONFIG) config.def.h vendor/linenoise/linenoise.h .build/jwt/libjwt.a
$(BUILD)/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(BIN): $(OBJECTS)
	$(CC) $(LDFLAGS) -o $@ $(OBJECTS) $(LDLIBS)

fixture:
	$(MAKE) CONFIG=tests/config.h BUILD=.build/test BIN=.build/harn-test

check: fixture
	python3 tests/check.py
	python3 tests/responses.py
	python3 tests/ui.py
	python3 tests/render.py
	python3 tests/job_control.py

check-streaming: fixture
	python3 tests/streaming.py

check-terminal: fixture
	python3 tests/terminal.py

check-release: release
	python3 tests/release.py dist/$(RELEASE).tar.gz

install: harn
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 755 harn $(DESTDIR)$(PREFIX)/bin/harn
	install -d $(DESTDIR)$(PREFIX)/share/licenses/harn
	install -m 644 LICENSE $(DESTDIR)$(PREFIX)/share/licenses/harn/LICENSE
	install -m 644 vendor/libjwt/LICENSE $(DESTDIR)$(PREFIX)/share/licenses/harn/LICENSE.libjwt
	install -m 644 vendor/linenoise/LICENSE $(DESTDIR)$(PREFIX)/share/licenses/harn/LICENSE.linenoise

release:
	$(MAKE) CONFIG=config.def.h BUILD=.build/release BIN=.build/harn-release
	rm -rf dist/$(RELEASE)
	mkdir -p dist/$(RELEASE)/bin dist/$(RELEASE)/licenses dist/$(RELEASE)/source/tests
	install -m 755 .build/harn-release dist/$(RELEASE)/bin/harn
	$(STRIP) dist/$(RELEASE)/bin/harn
	cp README.md DESIGN.md LICENSE dist/$(RELEASE)/
	cp vendor/libjwt/LICENSE dist/$(RELEASE)/licenses/LICENSE.libjwt
	cp vendor/linenoise/LICENSE dist/$(RELEASE)/licenses/LICENSE.linenoise
	cp $(filter-out vendor/%,$(SOURCES)) harn.h config.def.h Makefile README.md DESIGN.md LICENSE .clang-format .gitignore dist/$(RELEASE)/source/
	cp tests/*.py tests/config.h dist/$(RELEASE)/source/tests/
	cp -R vendor dist/$(RELEASE)/source/
	tar -C dist -czf dist/$(RELEASE).tar.gz $(RELEASE)

clean:
	rm -rf .build harn

.PHONY: all fixture check check-streaming check-terminal check-release install release clean
