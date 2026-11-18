"""Drop-in policy files: plain `.py` files with `def decide(state)` (TF-006).

Loading a policy file runs local code (REQ-205). Only the local CLI and in-process Python
may import this package; `service`, `mcp`, `application`, `domain`, `engines` and `state`
never do (D-139, boundary test).

Phase 5 ships only what DES dispatch needs: `des_runtime(file)` turns a
`DECISION = "dispatch"` file into a `twinflow.policy.PolicyRuntime`. The full loader,
policy view and checker (COMP-154 to COMP-156, Phase 3) replace `_load.py` and grow
`dispatch.py`'s state view; T-257 renames the package to `twinflow.extensions`.
"""

from twinflow.policy_files._load import PolicyFileError
from twinflow.policy_files.dispatch import DispatchState, des_runtime

__all__ = ["DispatchState", "PolicyFileError", "des_runtime"]
