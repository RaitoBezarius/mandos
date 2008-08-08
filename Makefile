WARN=-O -Wall -Wformat=2 -Winit-self -Wmissing-include-dirs -Wswitch-default -Wswitch-enum -Wunused-parameter -Wstrict-aliasing=2 -Wextra -Wfloat-equal -Wundef -Wshadow -Wunsafe-loop-optimizations -Wpointer-arith -Wbad-function-cast -Wcast-qual -Wcast-align -Wwrite-strings -Wconversion -Wstrict-prototypes -Wold-style-definition -Wpacked -Wnested-externs -Wunreachable-code -Winline -Wvolatile-register-var 
DEBUG=-ggdb3
# For info about _FORTIFY_SOURCE, see
# <http://gcc.gnu.org/ml/gcc-patches/2004-09/msg02055.html>
FORTIFY=-D_FORTIFY_SOURCE=2 # -fstack-protector-all
#COVERAGE=--coverage
OPTIMIZE=-Os
LANGUAGE=-std=gnu99

# Do not change these two
CFLAGS=$(WARN) $(DEBUG) $(FORTIFY) $(COVERAGE) $(OPTIMIZE) $(LANGUAGE)
LDFLAGS=$(COVERAGE)

DOCBOOKTOMAN=xsltproc --nonet --param man.charmap.use.subset "0" --param make.year.ranges "1" --param make.single.year.ranges "1" --param man.output.quietly "1"

PROGS=mandos-client plugins.d/password-request plugins.d/password-prompt
DOCS=mandos.8 mandos-client.8mandos plugins.d/password-request.8mandos plugins.d/password-prompt.8mandos mandos.conf.5 mandos-clients.conf.5

objects=$(shell for p in $(PROGS); do echo $${p}.o; done)

all: $(PROGS) $(DOCS)

%.5: %.xml
	cd $(shell dirname $^); $(DOCBOOKTOMAN) $(shell basename $^)

%.8: %.xml
	cd $(shell dirname $^); $(DOCBOOKTOMAN) $(shell basename $^)

%.8mandos: %.xml
	cd $(shell dirname $^); $(DOCBOOKTOMAN) $(shell basename $^)

mandos-client: mandos-client.o
	$(LINK.o) -lgnutls $(COMMON) $^ $(LOADLIBES) $(LDLIBS) -o $@

plugins.d/password-request: plugins.d/password-request.o
	$(LINK.o) -lgnutls -lavahi-core -lgpgme $(COMMON) $^ $(LOADLIBES) $(LDLIBS) -o $@

plugins.d/password-prompt: plugins.d/password-prompt.o
	$(LINK.o) $(COMMON) $^ $(LOADLIBES) $(LDLIBS) -o $@

.PHONY : all clean distclean run-client run-server

clean:
	-rm --force $(PROGS) $(objects) $(DOCS) core

distclean: clean
mostlyclean: clean
maintainer-clean: clean

check: all
	./mandos --check

run-client: all
	./mandos-client --plugin-dir=plugins.d --options-for=password-request:--keydir=keydir

run-server: all
	./mandos --debug --configdir=.
