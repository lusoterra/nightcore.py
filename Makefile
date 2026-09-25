PREFIX ?= /usr/local
BINDIR = $(PREFIX)/bin
BASHCOMPDIR = $(PREFIX)/share/bash-completion/completions
ZSHCOMPDIR = $(PREFIX)/share/zsh/site-functions
MANDIR = $(PREFIX)/share/man/man1

SRC = nightcore.py
BIN = nightcore
BASHCOMP = completions/nightcore.bash
ZSHCOMP = completions/_nightcore
MANPAGE = nightcore.1

.PHONY: all install uninstall check
all: check

check:
	python3 -c "compile(open('$(SRC)').read(), '$(SRC)', 'exec')"

install: check
	install -Dm755 $(SRC) $(DESTDIR)$(BINDIR)/$(BIN)
	install -Dm644 $(BASHCOMP) $(DESTDIR)$(BASHCOMPDIR)/$(BIN)
	install -Dm644 $(ZSHCOMP) $(DESTDIR)$(ZSHCOMPDIR)/_$(BIN)
	install -Dm644 $(MANPAGE) $(DESTDIR)$(MANDIR)/$(BIN).1

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/$(BIN)
	rm -f $(DESTDIR)$(BASHCOMPDIR)/$(BIN)
	rm -f $(DESTDIR)$(ZSHCOMPDIR)/_$(BIN)
	rm -f $(DESTDIR)$(MANDIR)/$(BIN).1
