# Alpaca examples from its documentation, not from its API

Every file here is an example Alpaca **publishes in its Trading API
documentation**, cut from the page verbatim. None was returned by an Alpaca
account. They stand in for recordings from Alpaca Paper, which the option work
(#2913) could not make while it was built without live data.

**Each is to be replaced by a recording.** A recording goes in
`tests/fixtures/alpaca/`, beside the other captures from a live paper account,
and the matching file here is deleted with the tests moved onto it.

What an example can prove is narrow: that this repo sends and reads the shape
the documentation shows. It cannot prove Alpaca's API answers that way. Each
`attribution.md` lists what its examples leave unshown.

## Regeneration

From `PythonDataService/`:

```bash
python -m scripts.fixture_generators.alpaca_documented_option_examples
python -m scripts.fixture_generators.alpaca_documented_option_activities   # option_activities/, and golden fixture OEV-001
```

Each fetches its pages and rewrites its JSON files. Neither uses an Alpaca key or
calls an Alpaca API. The `attribution.md` files are written by hand and carry
the date the pages were read.

These are outside the numerical golden manifest
(`tests/fixtures/golden/manifest.json`), as the captures in
`tests/fixtures/alpaca/` are: they pin a payload's shape, not a number.

Where an example's numbers are a reference output, they are held by a golden
fixture in the manifest, written from the same pages in the same run. `OEV-001`
(`tests/fixtures/golden/option-events/OEV-001/v1/`) holds the share trade Alpaca
prints beside each documented exercise and assignment.
