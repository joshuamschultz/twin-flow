# Business showcases

Three businesses, set up the way a real company would use twinflow: input files exported
from the ERP, one twin capsule that ties them together, a few short rule files, and a
notebook of commands with the answers saved in it. No code to write.

| Folder | The business | The decision |
|---|---|---|
| [`plastics-molding/`](plastics-molding/) | A plastics plant making 12 bins to stock on one press | Which rule for "what do I make next" earns the most? A race of rules, a custom score, a state map, and a new rule tested with `improve`. |
| [`cnc-job-shop/`](cnc-job-shop/) | A CNC job shop making parts to order on two shifts | Which open orders will be late, what is the bottleneck, and does a third machinist or a second mill fix it? |
| [`pump-supply/`](pump-supply/) | A pump plant that buys its parts through a slow port | Which customer orders can ship, which parts are short, and does the buyer's plan B fix it? |

Each folder has its own README and a notebook named after the folder.

## Each notebook has the same parts

1. **Your business:** who they are, the decision, and the money at stake.
2. **Your inputs:** each input file, with its columns explained.
3. **Check and run:** the `twinflow` commands, with their outputs.
4. **Your rules:** each rule file in a `%%writefile` box, with the key lines marked.
5. **Results and the decision:** what the commands said, in plain words.
6. **Try it yourself:** two or three small edits to try.

## Open a notebook

Jupyter is not part of twinflow. Start it for one session from this folder:

```bash
uv run --with jupyterlab jupyter lab
```

Then open a folder and its notebook. Run the boxes from top to bottom.

## How the notebooks are made

Each notebook is written as plain text in `_sources/<folder>.txt`. A `--- writefile`
cell is filled from the committed file, so a rule in a notebook is always the file in
the folder. To build the notebooks and run them, saving every output:

```bash
uv run --with nbclient --with ipykernel --with nbformat \
    python examples/showcase/build_notebooks.py
```

Each notebook runs in a fresh copy of its folder, so your folder does not change, and the
copy's path is replaced with `.` in the outputs. The test
`tests/integration/test_showcases.py` checks that each notebook matches its source, that
every file it reads exists, that every `%%writefile` box equals its file, and that every
`twinflow` command runs with exit code 0.
