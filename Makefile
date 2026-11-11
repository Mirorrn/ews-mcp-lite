# Local development against the grommunio test server (dev/grommunio/).
#
#   make setup-dev   one-time: install deps, build + seed grommunio, create .env
#   make dev         start grommunio and run the MCP server (Ctrl-C stops the server)
#   make stop        stop grommunio, keep its data
#   make test        unit tests

GROMMUNIO := dev/grommunio/.work/gromox-container
EWS_URL   := https://127.0.0.1:10443/EWS/Exchange.asmx

.PHONY: setup-dev dev stop test

setup-dev:
	uv sync
	dev/grommunio/setup.sh
	@if [ -f .env ]; then echo "keeping existing .env (compare with dev/grommunio/.env.example)"; \
	else cp dev/grommunio/.env.example .env && echo "created .env for grommunio"; fi

dev:
	@[ -d $(GROMMUNIO) ] || { echo "grommunio is not set up yet: run make setup-dev"; exit 1; }
	@[ -f .env ] || { echo ".env is missing: run make setup-dev"; exit 1; }
	cd $(GROMMUNIO) && docker compose up -d
	@echo "waiting for EWS at $(EWS_URL) ..."
	@for i in $$(seq 1 60); do \
	  [ "$$(curl -sk -m 5 -o /dev/null -w '%{http_code}' $(EWS_URL))" = "405" ] && exit 0; sleep 2; \
	done; echo "EWS did not come up"; exit 1
	uv run ews-mcp-lite

stop:
	@if [ -d $(GROMMUNIO) ]; then cd $(GROMMUNIO) && docker compose stop; else echo "grommunio is not set up"; fi

test:
	uv run pytest
