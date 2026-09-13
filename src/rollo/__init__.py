"""Rollo Code — 从头开始构建的最小编码智能体。"""

__version__ = "0.1.0"

# Public C03 control-plane entry point.  Importing it here keeps
# ``from rollo import Application`` useful without forcing callers to know the
# module layout; the implementation itself still requires an explicit
# ProjectContext at construction time.
from .application import Application, ApplicationResponse  # noqa: E402,F401

__all__ = ["Application", "ApplicationResponse", "__version__"]
