# One-line wrappers over run.sh (see it for what each target does).
.PHONY: install lint lint-ci test build check-placeholders validate-bundle

install:
	bash run.sh install

lint:
	bash run.sh lint

lint-ci:
	bash run.sh lint:ci

test:
	bash run.sh test

build:
	bash run.sh build

check-placeholders:
	bash run.sh check-placeholders $(if $(STRICT),--strict,)

validate-bundle:
	bash run.sh validate-bundle $(TARGET)
