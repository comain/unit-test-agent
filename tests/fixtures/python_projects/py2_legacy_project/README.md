# Python 2 Legacy Fixture

Tiny fixture repo for Phase 0 Python 2 support tests. It intentionally contains
Python 2-only syntax so later parser/runtime phases can distinguish legacy code
without importing target modules from Python 3.

Expected legacy command when Python 2 is available:

```bash
python2 -m unittest discover -s tests
```
