PYTHON ?= python3

.PHONY: help install test demo demo-stream demo-minimize clean

CRASHLAB = if command -v crashlab >/dev/null 2>&1; then crashlab

help:
	@echo "make install      - editable install with dev extras"
	@echo "make test         - run the pytest suite (fixtures must match expected.json)"
	@echo "make demo         - classify cases/ and print the human report"
	@echo "make demo-stream  - replay a case one byte at a time through the recognizer"
	@echo "make demo-minimize- shrink a failing case into an incident bundle under /tmp"

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

demo:
	@if command -v crashlab >/dev/null 2>&1; then \
		crashlab run cases --format human; \
	else \
		PYTHONPATH=src $(PYTHON) -m crashlab run cases --format human; \
	fi

demo-stream:
	@if command -v crashlab >/dev/null 2>&1; then \
		crashlab stream cases/v1/nested-object-valid --chunk-plan one-byte; \
	else \
		PYTHONPATH=src $(PYTHON) -m crashlab stream cases/v1/nested-object-valid --chunk-plan one-byte; \
	fi

demo-minimize:
	@if command -v crashlab >/dev/null 2>&1; then \
		crashlab minimize cases/v1/required-missing --out /tmp/crashlab-incident; \
	else \
		PYTHONPATH=src $(PYTHON) -m crashlab minimize cases/v1/required-missing --out /tmp/crashlab-incident; \
	fi

clean:
	rm -rf build dist .pytest_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
