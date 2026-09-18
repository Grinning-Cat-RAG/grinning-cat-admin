UID := $(shell id -u)
GID := $(shell id -g)
PWD = $(shell pwd)

LOCAL_DIR = $(PWD)/.venv/bin
PYTHON = $(LOCAL_DIR)/python
PYTHON3 = python3.11

help:  ## Show help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m\033[0m\n"} /^[$$()% a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

install: ## Update the local virtual environment with the latest requirements.
	@uv sync --link-mode=copy --frozen --no-install-project --no-upgrade --no-cache
	@uv cache clean
	@pip cache purge

update: ## Update and compile requirements for the local virtual environment.
	@uv sync --upgrade --link-mode=copy --no-install-project --no-cache
	@uv cache clean
	@pip cache purge
	@rm -rf *.egg-info

run:  ## Run the application client
	@$(PYTHON) -m streamlit run app/main.py

dev:
	git pull
	docker pull dhi.io/python:3.13
	docker pull dhi.io/python:3.13-dev
	uv pip compile -U -o requirements.txt pyproject.toml
	docker buildx build . -f Dockerfile:dhi -t grinning-cat-admin:dev

check: ## Check requirements for the local virtual environment.
	@uv sync --check