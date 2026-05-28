.PHONY: check deploy-function-dry-run

check:
	./scripts/check.sh

deploy-function-dry-run:
	DRY_RUN=1 ./scripts/deploy-yandex-function.sh --skip-build
