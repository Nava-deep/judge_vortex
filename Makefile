.PHONY: start stop start-codespaces migrate makemigrations check test test-fast kafka-topics autoscale-once autoscale-loop

PYTHON ?= python3

start:
	./start_vortex.sh

stop:
	./stop_vortex.sh

start-codespaces:
	./start_codespaces.sh

migrate:
	$(PYTHON) manage.py migrate

makemigrations:
	$(PYTHON) manage.py makemigrations

check:
	$(PYTHON) manage.py check

test:
	$(PYTHON) manage.py test

test-fast:
	DB_ENGINE=sqlite $(PYTHON) manage.py test core_api.tests realtime.tests

kafka-topics:
	KAFKA_BOOTSTRAP_SERVERS=$${KAFKA_BOOTSTRAP_SERVERS:-127.0.0.1:9092} $(PYTHON) kafka_setup.py

autoscale-once:
	$(PYTHON) infrastructure/autoscaler/autoscale_executors.py --once

autoscale-loop:
	$(PYTHON) infrastructure/autoscaler/autoscale_executors.py
