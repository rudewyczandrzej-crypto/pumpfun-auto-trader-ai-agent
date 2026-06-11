# Railway / Railpack Python 3.11 patch

Add these files to the root of your GitHub repo:

.python-version
runtime.txt
.tool-versions
mise.toml
railpack.json

Also set Railway Variable:

RAILPACK_PYTHON_VERSION=3.11

If you have old variables, remove them:
NIXPACKS_PYTHON_VERSION
PYTHON_VERSION if it points to 3.13
RAILPACK_PYTHON_VERSION if it points to 3.13

Then:
1. Commit changes
2. Railway -> Redeploy
3. If it still uses Python 3.13, clear build cache / redeploy without cache if Railway shows that option.

Reason:
Railpack defaults to Python 3.13.x if not pinned. This patch pins Python 3.11 in every common format Railpack/mise can read.
