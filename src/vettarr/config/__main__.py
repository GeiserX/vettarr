"""``python -m vettarr.config check|import-env|example``.

Same commands as ``vettarr config ...``, without importing the rest of
the app.
"""

import sys

from vettarr.config.cli import main

sys.exit(main())
