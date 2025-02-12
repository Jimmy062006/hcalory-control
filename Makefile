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
	uv export --refresh --no-hashes -o requirements.txt
	uv export --refresh --only-dev --no-hashes -o requirements-dev.txt

upgrade-dependencies:
	# probably a dumb way to do this
	uv export -U --refresh --no-hashes -o requirements.txt
	uv export -U --refresh --only-dev --no-hashes -o requirements-dev.txt

clean:
	rm -r ./dist

build:
	uv build

publish: build
	uv publish
