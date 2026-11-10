import os
import sys

if sys.version_info < (3, 10):  # noqa: UP036 - the check is for interpreters older than the minimum
    sys.exit('goproxy-gh-pages needs Python 3.10 or newer')

if not __package__:
    # run as `python -I path/to/goproxy_gh_pages`: isolated mode keeps the working directory, which
    # may have files named like standard modules, out of sys.path, so the package adds its parent
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goproxy_gh_pages.cli import main

sys.exit(main())
