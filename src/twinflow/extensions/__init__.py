"""Drop-in extensions: plain Python files a company adds to a twin project (TF-006).

One framework for every drop-in kind (D-150): policies, rewards, views, features, demand,
observations and engines. Each file holds one named function (or, for an engine, one
class); built-ins ship in the same format under `builtin/` and can be copied and edited
(D-142). A project file with the same `NAME` as a built-in replaces it (D-151).

Loading an extension file runs local code (REQ-205). Only the local CLI and in-process
Python may import this package; `service`, `mcp`, `application`, `domain`, `scenario`,
`engines` and `state` never do (D-139, boundary test).
"""
