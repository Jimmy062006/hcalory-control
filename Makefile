.PHONY: lint fix format export-dependencies clean

default: format

lint:
	ruff check
	mypy .

fix:
	ruff check --fix

format:
	ruff check --select I --fix
	ruff format

export-dependencies:
	uv export --no-hashes -o requirements.txt
	uv export --only-dev --no-hashes -o requirements-dev.txt

clean:
	rm -r ./dist

build:
	uv build

publish: build
	uv publish
