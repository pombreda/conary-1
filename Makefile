#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


all: subdirs

export TOPDIR = $(shell pwd)
export VERSION = 2.5.0
export DISTDIR = $(TOPDIR)/conary-$(VERSION)
export prefix = /usr
export lib = $(shell uname -m | $(SED) -r '/x86_64|ppc64|s390x|sparc64/{s/.*/lib64/;q};s/.*/lib/')
export bindir = $(prefix)/bin
export libdir = $(prefix)/$(lib)
export libexecdir = $(prefix)/libexec
export datadir = $(prefix)/share
export mandir = $(datadir)/man
export sitedir = $(libdir)/python$(PYVER)/site-packages/
export conarydir = $(sitedir)/conary
export conarylibdir = $(libdir)/conary
export conarylibexecdir = $(libexecdir)/conary

minimal:
	NO_KID=1 $(MAKE) all


SUBDIRS = commands conary config extra man scripts

extra_files = \
	LICENSE			\
	Make.rules 		\
	Makefile		\
	NEWS			\

dist_files = $(extra_files)

.PHONY: clean dist install subdirs

subdirs: default-subdirs

install: install-subdirs

dist:
	if ! grep "^Changes in $(VERSION)" NEWS > /dev/null 2>&1; then \
		echo "no NEWS entry"; \
		exit 1; \
	fi
	$(MAKE) forcedist

archive:
	@rm -rf /tmp/conary-$(VERSION) /tmp/conary$(VERSION)-tmp
	@mkdir -p /tmp/conary-$(VERSION)-tmp
	@git archive --format tar $(VERSION) | (cd /tmp/conary-$(VERSION)-tmp/ ; tar x )
	@mv /tmp/conary-$(VERSION)-tmp/ /tmp/conary-$(VERSION)/
	@dir=$$PWD; cd /tmp; tar -c --bzip2 -f $$dir/conary-$(VERSION).tar.bz2 conary-$(VERSION)
	@rm -rf /tmp/conary-$(VERSION)
	@echo "The archive is in conary-$(VERSION).tar.bz2"

version:
	$(SED) -i 's/@NEW@/$(VERSION)/g' NEWS
	$(SED) -i 's/@NEW@/$(VERSION)/g' ./doc/PROTOCOL.versions

show-version:
	@echo $(VERSION)

smoketest: archive
	@echo "=== sanity building/testing conary ==="; \
	tar jxf $(DISTDIR).tar.bz2 ; \
	cd $(DISTDIR); \
	make > /dev/null; \
	tmpdir=$$(mktemp -d); \
	make install DESTDIR=$$tmpdir > /dev/null; \
	PYTHONPATH=$$tmpdir/usr/lib/python$(PYVER)/site-packages $$tmpdir/usr/bin/conary --version > /dev/null || echo "CONARY DOES NOT WORK"; \
	PYTHONPATH=$$tmpdir/usr/lib/python$(PYVER)/site-packages $$tmpdir/usr/bin/cvc --version > /dev/null || echo "CVC DOES NOT WORK"; \
	cd -; \
	rm -rf $(DISTDIR) $$tmpdir

forcedist: $(dist_files) smoketest

tag:
	git tag $(VERSION) refs/heads/master

docs:
	cd scripts; ./gendocs

clean: clean-subdirs default-clean
	$(MAKE) -C conary_test clean

check: check-subdirs

# Build extension (cython) output, which is checked into source control and
# thus not normally built.
ext:
	make -C conary/lib/ext ext

ext-clean:
	make -C conary/lib/ext ext-clean


ccs: dist
	cvc co --dir conary-$(VERSION) conary=conary.rpath.com@rpl:devel
	$(SED) -i 's,version = ".*",version = "$(VERSION)",' \
                                        conary-$(VERSION)/conary.recipe;
	$(SED) -i 's,version = '.*',version = "$(VERSION)",' \
                                        conary-$(VERSION)/conary.recipe;
	$(SED) -i 's,r.addArchive(.*),r.addArchive("conary-$(VERSION).tar.bz2"),' \
                                        conary-$(VERSION)/conary.recipe;
	# Assume conary tip always has the patches required to build from the
	# recipe: filter out non-sqlite patches (the sqlite patch spans across
	# two lines)
	$(SED) -i 's,r.addPatch(.*),,' conary-$(VERSION)/conary.recipe;
	cp conary-$(VERSION).tar.bz2 conary-$(VERSION)
	# This is just to prime the cache for the cook from a recipe
	bin/cvc cook --build-label conary.rpath.com@rpl:devel --prep conary=conary.rpath.com@rpl:devel
	bin/cvc cook --build-label conary.rpath.com@rpl:devel conary-$(VERSION)/conary.recipe
	rm -rf conary-$(VERSION)

include Make.rules
