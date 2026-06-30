.PHONY: install lint type test fmt run-api run-bot run-scheduler up down logs

install:
	pip install -e ".[dev]"
	python -m playwright install --with-deps chromium

lint:
	ruff check src tests

fmt:
	ruff format src tests
	ruff check --fix src tests

type:
	mypy src

test:
	pytest -q

run-api:
	uvicorn src.api.main:app --reload --port 8000

run-bot:
	python -m src.bot.main

run-scheduler:
	python -m src.scheduler.dispatcher

up:
	docker compose -f deploy/docker-compose.yml up -d --build

down:
	docker compose -f deploy/docker-compose.yml down

logs:
	docker compose -f deploy/docker-compose.yml logs -f --tail=200
