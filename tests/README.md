# Tests

```bash
python tests/run_all.py
```

That runs every suite and prints a pass or fail line each. `-v` shows what each one checked;
a name fragment runs a subset, so `python tests/run_all.py logging` runs the two logging
suites. A single suite is just a script:

```bash
python tests/test_autorole.py
```

## What these are

Plain scripts that assert their way through a feature and print what they checked, not pytest
modules. There is no test runner to install and no fixtures to learn: each file sets up fake
Discord objects and a fake Mongo, calls the real cog, and checks what came out.

They need the same dependencies as the bot (`pip install -r requirements.txt`), because they
import the real `discord.py` and build real embeds. Mongo is faked, so nothing touches a
database and no credentials are needed.

`run_all.py` runs each suite in its own process. Several of them install fake `pymongo`,
`discord` and `Database` modules into `sys.modules`, so sharing an interpreter would let one
suite's fakes leak into the next.

## What they are good at

Bugs that only appear when the code actually runs. The ones these caught, all of which read
fine in review:

- `/welcome set` crashed with a `TypeError` because a header and the rendered greeting were
  both passed as `content`
- `Optional` was used but never imported in `Ratings`, which would have stopped the cog loading
- an `await` on a synchronous pymongo call was swallowed by a broad `except`, so a cohort was
  re-pinged up to twelve times a day
- a role button's `custom_id` was trusted on the way back, which would have let anyone grant
  themselves any role in the server

## Adding one

Copy the nearest existing suite. The shape is always: fake the collections, fake the Discord
objects, call the cog method, assert on what it produced. Print a line per check so a passing
run still says what it covered.

Two conventions worth keeping:

- **Assert on behaviour, not on wording.** `assert "above my own" in reply` survives a copy
  edit; comparing the whole sentence does not.
- **Test the refusals.** Most of the value here is in the cases where something should *not*
  happen: the permission that is missing, the id from another server, the event that is
  switched off.
