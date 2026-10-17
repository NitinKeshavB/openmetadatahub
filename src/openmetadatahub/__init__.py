"""openmetadatahub: governance, data quality, data contracts and AI governance for Databricks."""

from importlib.metadata import (
    PackageNotFoundError,
    version,
)

try:
    __version__ = version("openmetadatahub")  # version.txt is the single source, read by setuptools at build time
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
