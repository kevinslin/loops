PYTHON ?= python

.PHONY: test test-integ-live install

test:
	$(PYTHON) -m pytest

test-integ-live:
	LOOPS_INTEG_LIVE=1 $(PYTHON) -m pytest tests/integ -k outer_loop_pickup_live -s

install:
	$(PYTHON) -m pip install -e .
