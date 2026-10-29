# Thin wrapper: every target is one docker compose command. tasks.py has the same targets
# for machines without make (python tasks.py <target>).
RUN = docker compose run --rm app

.PHONY: up down build migrate test sim loadtest psql shell

up:
	docker compose up -d --build db
	docker compose build app

down:
	docker compose down -v

build:
	docker compose build app

migrate:
	$(RUN) python -m smartdialer.migrate

test:
	$(RUN) python -m pytest -q

sim:
	$(RUN) python -m sim.run_all

loadtest:
	$(RUN) python -m loadtest.run $(ARGS)

demo-%:
	$(RUN) python -m demos.$(subst -,_,$*)

psql:
	docker compose exec db psql -U dialer dialer

shell:
	$(RUN) bash
