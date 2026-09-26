# Contributing

Thanks for helping. Data recovery tools are only as good as the formats and failure
cases they have seen.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,all]"
pytest
ruff check src tests
```

Integration tests run against a real ZFS image when `ZESUS_TEST_IMAGE` is set.

## Using AI tools

AI-assisted contributions are welcome. You remain responsible for everything you submit:
test it, review it, and keep commit messages factual. See [AI_POLICY.md](AI_POLICY.md).

## Ground rules

* **The source is sacred.** Nothing may write to the evidence. All access goes through
  `zesus.io.RawSource`, and `tests/unit/test_guard.py` enforces the rule. Pull
  requests that add write paths to `zesus/io` will not be merged.
* **Never guess silently.** Anything unverified or inferred must be labelled as such in
  the map and in output reports.
* **Be damage-tolerant.** Parsers must survive truncated, zeroed and random input. Log
  what could not be read and continue.
* New filesystem support belongs in a plugin (see `docs/writing-plugins.md`).
* Add tests with small real fixtures where possible. Scripts that generate fixtures go in
  `dev/`.

## Reporting a recovery problem

Please include:
* the output of `zesus info MAP`;
* the JSON log (`*.log.jsonl`);
* your OpenZFS version and pool features (`zpool get all`, if the pool still imports).

Never attach disk images or maps from real cases publicly. They contain your data.
