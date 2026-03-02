PYTHON ?= python

.PHONY: test test-integ-live test-integ-end2end install

test:
	$(PYTHON) -m pytest

test-integ-live:
	LOOPS_INTEG_LIVE=1 $(PYTHON) -m pytest tests/integ -k outer_loop_pickup_live -s

test-integ-end2end:
	LOOPS_INTEG_END2END=1 $(PYTHON) -m pytest tests/integ -k end2end_live -s

install:
	$(PYTHON) -m pip install -e .
