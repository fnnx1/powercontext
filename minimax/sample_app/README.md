# Pricing sample app

This tiny business project exists to demonstrate the SDD loop: the target tests
for the volume-discount requirement fail before the MiniMax-generated change and
pass afterwards, while the regression tests pass in both states.

Run from the `minimax/` directory:

```bash
.venv/bin/python -m pytest -q sample_app/tests
```
