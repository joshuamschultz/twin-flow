# Install

## Engine (core)

Python 3.11 or newer.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

That gives you the `twinflow` command and the library. The core depends only on
simpy, numpy, polars, scipy, pyyaml, simpleeval, plotly, and openpyxl.

To consume a reviewed source revision from another project, pin the Git commit in the
requirement rather than installing a moving branch:

```text
twinflow @ git+ssh://git@github.com/joshuamschultz/twin-flow.git@<release-commit>
```

The typed `twinflow.production` contracts, deterministic baseline scheduler, verifier,
state ledgers, and runtime are included in the core install. The `scheduling` extra is
for the separate legacy CP-SAT scheduler and is not required by the baseline.

## API extra (the REST service)

The service (`twinflow.service`) and the `twinflow serve` command need FastAPI and
uvicorn. The `dev` extra already includes them; a lean production install can add just
the `api` extra:

```bash
pip install -e ".[api]"
```

The core engine never imports the service, so a plain install stays dependency-light.

## Front end

Node 22.12+ and npm.

```bash
cd web
npm install
```

See [frontend.md](frontend.md) to run it.

## Verifying the install

```bash
twinflow validate examples/cnc-shop/model.yaml    # should print nothing, exit 0
pytest -q                                          # the full test suite
pytest -q tests/production                         # typed production capability checks
ruff check src tests                               # lint gate
mypy                                               # strict type gate
```
