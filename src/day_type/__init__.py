"""Day-type classifier — the news-calendar spine.

Replaces the regime-classifier spine. The classifier itself is a pure
function over the news_calendar cache and the NY-session anchor used
by the existing risk-layer EOD logic.

Public surface:
    DayType            — enum (day_type/labels)
    classify_day_type  — pure-function classifier (day_type/classifier)
"""
# Import order matters: ``labels.DayType`` must be bound on this module
# BEFORE ``classifier`` is imported. ``classifier`` transitively triggers
# ``risk.types`` (via ``risk.news_calendar`` → ``risk`` package init →
# ``risk.types``), and ``risk.types`` does ``from day_type import DayType``.
# When that re-import happens while this ``__init__`` is still running,
# Python returns the partially-initialised ``day_type`` module — so the
# name must already be on it.
from .labels import DayType
from .classifier import classify_day_type

__all__ = ["DayType", "classify_day_type"]
