.PHONY: install preflight start status verify stop test pake release official-prepare official-start official-status official-stop official-update official-schedule official-schedule-status

install:
	./install.sh

preflight:
	./preflight.sh

start:
	./start.sh

status:
	./status.sh

verify:
	./verify.sh

stop:
	./stop.sh

test:
	python3 -m unittest discover -s tests

pake:
	./build_pake_prototype.sh

release:
	./package_release.sh

official-prepare:
	./manage_official_openclaw.sh prepare

official-start:
	./manage_official_openclaw.sh start

official-status:
	./manage_official_openclaw.sh status

official-stop:
	./manage_official_openclaw.sh stop

official-update:
	./manage_official_openclaw.sh update

official-schedule:
	./manage_official_openclaw.sh install-schedule

official-schedule-status:
	./manage_official_openclaw.sh schedule-status
