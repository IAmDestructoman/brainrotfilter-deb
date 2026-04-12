# BrainrotFilter Makefile
# Build the .deb package and run tests

PACKAGE_NAME = brainrotfilter
VERSION = $(shell python3 -c "exec(open('src/brainrotfilter/version.py').read()); print(__version__)")

.PHONY: help build-deb clean test lint install

help:
	@echo "BrainrotFilter Makefile"
	@echo ""
	@echo "Targets:"
	@echo "  build-deb   Build the Debian package"
	@echo "  test        Run unit tests"
	@echo "  lint        Run linting (flake8)"
	@echo "  clean       Remove build artifacts"
	@echo "  install     Install into local venv (development)"
	@echo ""

build-deb:
	dpkg-buildpackage -us -uc -b
	@echo ""
	@echo "Package built. Look for ../$(PACKAGE_NAME)_*.deb"

clean:
	rm -rf build/ dist/ *.egg-info
	rm -rf debian/brainrotfilter debian/.debhelper debian/files
	rm -f debian/debhelper-build-stamp debian/*.substvars debian/*.debhelper.log
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true

test:
	python3 -m pytest tests/ -v --tb=short

lint:
	python3 -m flake8 src/brainrotfilter/ --max-line-length=120 --ignore=E501,W503,E402

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	.venv/bin/pip install -e .
