PYTHON ?= .venv/bin/python
CLUSTER ?= alert-enrichment-demo
IMAGE ?= alert-enrichment:dev

.PHONY: install test lint typecheck check run demo e2e kind-up kind-down docker-build helm-lint helm-template clean

install:
	python3.13 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

lint:
	$(PYTHON) -m ruff check src tests

typecheck:
	$(PYTHON) -m mypy src

test:
	$(PYTHON) -m pytest --cov --cov-report=term-missing

check: lint typecheck test

run:
	$(PYTHON) -m alert_enrichment

demo:
	./demo/run.sh

# Requires a kind cluster with demo/broken-app.yaml applied (or run demo/run.sh).
e2e:
	RUN_E2E=1 $(PYTHON) -m pytest -m e2e -v

kind-up:
	kind create cluster --name $(CLUSTER) --wait 120s
	kubectl apply -f demo/broken-app.yaml

kind-down:
	kind delete cluster --name $(CLUSTER)

docker-build:
	docker build -t $(IMAGE) .

helm-lint:
	helm lint deploy/helm/alert-enrichment

helm-template:
	helm template alert-enrichment deploy/helm/alert-enrichment --kube-version 1.33.0

clean:
	rm -rf var .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
