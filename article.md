# Your Pytest Tests Are Running Blind. Here's How to Fix That.

You run your test suite. A test takes 45 seconds. It fails. You stare at the terminal output, scrolling through a wall of text, trying to figure out *which step* actually broke — and how long each one took.

Sound familiar?

I've been there more times than I'd like to admit. And honestly, the default pytest output is great for telling you *what* failed. It's terrible at telling you *where you are right now*.

So I built a plugin to fix that.

---

## The problem

Let's say you have a test like this:

```python
@allure.step("Create booking")
def create_booking():
    time.sleep(3)

@allure.step("Call API")
def call_api():
    time.sleep(3)

@allure.step("Validate response")
def validate_response():
    time.sleep(2)

def test_checkout():
    create_booking()
    call_api()
    validate_response()
```

When you run this test, pytest tells you... nothing. For 8 full seconds. Then it either says `PASSED` or `FAILED`. If it fails, you get a traceback. But during those 8 seconds? Silence. You're left wondering: did it hang? Is `Create booking` still running? Did `Call API` even start?

This gets worse with longer tests. And it gets *much* worse when you're running tests in parallel with `pytest-xdist`, where multiple tests run at the same time with zero visibility into what's happening inside each one.

---

## pytest-step-logger

[pytest-step-logger](https://github.com/Slaaayer/pytest-step-logger) is a pytest plugin that renders test steps as a live, colour-coded tree directly in your terminal. In real time.

Here's what it looks like mid-execution:

```
⟳ test_checkout
├── setup
│   ├── ✔ db_connection  0.30s
│   └── ✔ user_session   0.15s
├── ✔ Create booking     3.01s
├── ▶ Call API            1.24s   ← running right now
└── ○ Validate response           ← pending
```

Steps transition through four states:

| Symbol | Colour | Meaning |
|--------|--------|---------|
| `○` | grey | Pending — not reached yet |
| `▶` | yellow | Running — currently executing |
| `✔` | green | Passed |
| `✘` | red | Failed |

When the test completes, the tree freezes in its final state:

```
✔ test_checkout
├── setup
│   ├── ✔ db_connection   0.30s
│   └── ✔ user_session    0.15s
├── ✔ Create booking      3.01s
├── ✔ Call API             3.00s
├── ✔ Validate response    2.00s
└── teardown
    ├── ✔ user_session    0.10s
    └── ✔ db_connection   0.20s
```

Fixtures get their own `setup` and `teardown` sections. You see exactly what ran, how long it took, and whether it passed — for every single step, including the infrastructure around your test.

---

## Installation and usage

```bash
pip install pytest-step-logger
```

Then just add one flag:

```bash
pytest --step-log
```

That's it. No configuration files, no code changes, no decorators to add. The plugin hooks into pytest's execution lifecycle and does everything automatically.

If you want it on by default:

```toml
# pyproject.toml
[tool.pytest.ini_options]
addopts = "--step-log"
```

### Optional integrations

The core plugin only needs `pytest` and `rich`. But it plays well with others:

```bash
# Everything at once
pip install "pytest-step-logger[all]"

# Or pick what you need
pip install "pytest-step-logger[allure]"   # @allure.step support
pip install "pytest-step-logger[check]"    # pytest-check soft assertions
pip install "pytest-step-logger[xdist]"    # parallel execution
```

---

## How it actually works

The plugin adapts to your project automatically. Two modes, zero configuration.

**If you use `@allure.step`**, it hooks into allure's own event system and picks up step names from your decorators. **If you don't use allure**, it falls back to tracking plain function calls and uses function names instead. Either way, you get the same live tree — no code changes needed.

Before the test even runs, the plugin scans the test function to discover all the steps it *will* call, and pre-builds the tree with grey pending nodes. So you see the full execution plan upfront, not just what already happened.

**Fixtures** are tracked too. The plugin times each fixture's setup and teardown phases and displays them in their own branches. Internal pytest fixtures (`request`, `tmp_path`, etc.) are filtered out — you only see yours.

**Soft assertions** from [pytest-check](https://github.com/okken/pytest-check) are detected as well. If a step has soft failures, it turns red even though no exception propagated.

**Parallel execution** with `pytest-xdist` was the trickiest part. Workers have no terminal, so step records are serialised and sent to the controller process, which manages a single live display — animated spinners with elapsed times for running tests, and completed trees printed above as they finish:

```
  ⠋ test_checkout        2.3s
  ⠙ test_cancel_booking  1.1s
  ⠹ test_search          0.4s
```

No background threads. Rich's refresh loop handles everything.

---

## Why this matters

**During development**, you want to know where your test *is*, not just where it *was*. A 30-second test that hangs on step 2 looks identical to a 30-second test that hangs on step 5 — unless you have visibility.

**During CI debugging**, the final tree output tells you exactly which step failed and how long each step took. No more guessing whether the slowdown is in the database fixture or the API call.

**During parallel runs**, you stop flying blind. You see all your workers, what they're doing, and how long they've been doing it.

**For test design**, the timing breakdown reveals bottlenecks. Maybe your `Create booking` step takes 3 seconds because it's hitting a real API when it should be mocked. Maybe your database fixture takes longer than the test itself. You can't optimise what you can't see.

---

## A note on how this was built

This tool was built with the help of [Claude](https://claude.ai) - who wrote tests, questioned every design decision, and occasionally suggested abstractions that were suspiciously too clean. The bugs are mine. The elegant storage interface is probably Claude's. We make a good team, even if one of us doesn't drink coffee.
---

## Get started

```bash
pip install pytest-step-logger
pytest --step-log
```

GitHub: [github.com/Slaaayer/pytest-step-logger](https://github.com/Slaaayer/pytest-step-logger)
PyPI: [pypi.org/project/pytest-step-logger](https://pypi.org/project/pytest-step-logger/)

If you find it useful, a star on GitHub goes a long way. If you find a bug, open an issue — I'll probably fix it with Claude.
