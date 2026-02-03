PYTHON ?= python

.PHONY: test

test:
	$(PYTHON) -m pytest
