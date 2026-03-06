.PHONY: install preflight start status verify stop test pake release

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
