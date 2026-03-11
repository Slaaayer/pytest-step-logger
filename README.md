# pytest-step-logger

Live, colour-coded step trees in your pytest terminal — powered by [Rich](https://github.com/Textualize/rich).

```
⟳ test_checkout
├── ✔ Create booking   3.01s
├── ▶ Call API         1.24s   ← running right now
└── ○ Validate response        ← pending
```

Steps transition in real time:

| Symbol | Colour | Meaning        |
|--------|--------|----------------|
| `○`    | grey   | Not yet reached |
| `▶`    | yellow | Currently running |
| `✔`    | green  | Passed          |
| `✘`    | red    | Failed          |

---

## Features

- **Allure support** — step labels come from `@allure.step("label")` when available.
- **Plain-function fallback** — when there is no allure, function names are used automatically (via `sys.settrace`).
- **Ahead-of-time tree** — all steps appear as grey nodes *before* execution starts, so you see the full plan upfront.
- **Soft-assertion aware** — failures from [pytest-check](https://github.com/okken/pytest-check) are detected and colour the step red even though no exception propagates.
- **Caught-exception fix** — a step that catches and swallows an exception is correctly left green.
- **pytest-xdist parallel support** — a single persistent spinner panel shows every currently-running test with elapsed times; completed trees print above the panel without flicker.

---

## Installation

```bash
pip install pytest-step-logger
```

With optional integrations:

```bash
# allure + pytest-check + xdist all at once
pip install "pytest-step-logger[all]"

# individual extras
pip install "pytest-step-logger[allure]"
pip install "pytest-step-logger[check]"
pip install "pytest-step-logger[xdist]"
```

---

## Usage

Add `--step-log` to your pytest invocation:

```bash
pytest --step-log
```

Or make it permanent in `pyproject.toml` / `pytest.ini`:

```toml
[tool.pytest.ini_options]
addopts = "--step-log"
```

### With allure steps

```python
import allure

@allure.step("Create booking")
def create_booking():
    ...

@allure.step("Call API")
def call_api():
    ...

def test_checkout():
    create_booking()
    call_api()
```

### With plain functions (no allure)

```python
def create_booking():
    ...

def call_api():
    ...

def test_checkout():
    create_booking()
    call_api()
```

The plugin automatically detects the absence of allure and falls back to function names.

### Parallel execution (pytest-xdist)

```bash
pytest --step-log -n auto
```

While tests run in parallel you see a live spinner panel:

```
  ⠋ test_checkout        2.3s
  ⠙ test_cancel_booking  1.1s
  ⠹ test_search          0.4s
```

As each test finishes its full step tree is printed above the panel.

---

## How it works

| Scenario | Step source | Live display |
|----------|-------------|--------------|
| Sequential, allure | `@allure.step` hook via `allure_commons.plugin_manager` | Per-test `Live` tree |
| Sequential, no allure | `sys.settrace` call/return events | Per-test `Live` tree |
| xdist worker | Same as above (no TTY needed) | None — serialised to report |
| xdist controller | Deserialises worker reports | Persistent spinner + printed trees |

Step records are serialised as JSON into `report.user_properties` and survive xdist's wire protocol transparently.

---

## Requirements

- Python ≥ 3.11
- pytest ≥ 7.0
- rich ≥ 13.0

Optional:
- allure-pytest ≥ 2.13
- pytest-check ≥ 2.7.4
- pytest-xdist ≥ 3.8.0

---

## License

[MIT](LICENSE)
