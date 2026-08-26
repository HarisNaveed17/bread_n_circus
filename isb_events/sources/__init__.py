"""Event sources.

Importing this package imports each scraper module so its `@register` decorator
runs and populates the registry. Add new scrapers to the imports below.
"""

from . import (
    base,  # noqa: F401
    blackhole,  # noqa: F401
    ticketwala,  # noqa: F401
)

# Scraper modules register themselves on import. Uncomment as they land.
