"""grib_nomad — friendly NOMADS GRIB downloader for weather routing."""

from grib_nomad.core.recipe import CategoryPlan, ModelTier, Recipe
from grib_nomad.core.regions import REGIONS, BoundingBox, get_region

__version__ = "0.1.0.dev0"

__all__ = [
    "BoundingBox",
    "CategoryPlan",
    "ModelTier",
    "REGIONS",
    "Recipe",
    "__version__",
    "get_region",
]
