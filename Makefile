DATABASE_URL ?= postgresql://fello:fello@localhost/fello_health

.PHONY: data validate load stage resolve evaluate core features train test all
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
core:      ## attribute every fact to an account; renewal labels
	DATABASE_URL=$(DATABASE_URL) python -m core.build_core
features:  ## point-in-time training snapshots
	DATABASE_URL=$(DATABASE_URL) python -m features.build_features
train:     ## fit + walk-forward backtest -> model/BACKTEST.txt
	DATABASE_URL=$(DATABASE_URL) python -m model.train
test:      ## loader tests (needs a scratch DB: postgresql://fello:fello@localhost/fello_test)
	python -m unittest discover -s tests -t . -v
all: data load
