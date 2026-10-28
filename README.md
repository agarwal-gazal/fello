# GTM health scoring: take-home (work in progress)

Status: day 1 of 4. Data generation and raw ingestion are done; everything else is in progress.
The real README (decisions, what was skipped, what breaks at 10x) is distilled from `DECISIONS.md` at the end.

## Run it
```
make data      # synthetic data, deterministic, ~40 s
make validate  # prints the evidence that the data is realistic and carries signal
make load      # raw files -> Postgres raw.*, idempotent (needs DATABASE_URL)
make test
```
See `data_gen/DATA_DICTIONARY.md` for what the data contains and the mess in it, and `DECISIONS.md` for why.
