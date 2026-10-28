DATABASE_URL ?= postgresql://fello:fello@localhost/fello_health

.PHONY: data validate load stage resolve evaluate test all
data:      ## generate the synthetic raw data (deterministic, ~40s)
	python -m data_gen.generate --out data --seed 7
validate:  ## evidence that the data is realistic and carries signal
	python -m data_gen.validate --data data
load:      ## load raw files into Postgres (idempotent; safe to re-run)
	DATABASE_URL=$(DATABASE_URL) python -m ingest.load_raw --data data
stage:     ## typed, deduplicated staging layer
	DATABASE_URL=$(DATABASE_URL) python -m ingest.build_staging
resolve:   ## one account id across every source
	DATABASE_URL=$(DATABASE_URL) python -m resolve.resolve
evaluate:  ## score entity resolution against the hidden answer key
	DATABASE_URL=$(DATABASE_URL) python -m resolve.evaluate --data data --out resolve/EVALUATION.txt
test:      ## loader tests (needs a scratch DB: postgresql://fello:fello@localhost/fello_test)
	python -m unittest discover -s tests -t . -v
all: data load
