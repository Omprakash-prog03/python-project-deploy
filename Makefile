.PHONY: help install dev run test lint eval docker compose seed init-db smoke postgres clean

help:
	@echo "PlateVision targets:"
	@echo "  make install   install runtime + dev dependencies"
	@echo "  make dev       run the dev server with autoreload (port 8000)"
	@echo "  make run       run the production-style server"
	@echo "  make test      run the test suite"
	@echo "  make lint      ruff check"
	@echo "  make eval      ALPR accuracy harness (clean frames)"
	@echo "  make eval-deg  ALPR accuracy harness (degraded frames)"
	@echo "  make docker    build the production image"
	@echo "  make compose   run the full stack with docker compose"
	@echo "  make init-db   prepare the schema (run before workers, safe to repeat)"
	@echo "  make smoke     end-to-end smoke test against a running instance"
	@echo "  make seed      provision the demo workspace"
	@echo "  make clean     remove caches and local storage"

install:
	python -m pip install -r requirements-dev.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run:
	uvicorn app.main:app --host 0.0.0.0 --port $${PORT:-8000} --workers 2 --proxy-headers

test:
	pytest -q

lint:
	ruff check app tests scripts

eval:
	python scripts/eval_alpr.py

eval-deg:
	python scripts/eval_alpr.py --degraded

docker:
	docker build -t platevision/api:local .

compose:
	docker compose up --build

init-db:
	python scripts/init_db.py

smoke:
	python scripts/smoke.py --base $${BASE:-http://localhost:8000}

seed:
	python -m app.seed

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ */__pycache__ */*/__pycache__
	rm -rf storage/platevision.db storage/uploads/* storage/models/*.npz
