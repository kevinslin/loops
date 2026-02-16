PYTHON ?= python

.PHONY: test install

test:
	$(PYTHON) -m pytest

install:
	$(PYTHON) -m pip install -e .
