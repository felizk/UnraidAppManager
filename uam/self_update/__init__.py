"""UAM updating its own container (see `service.py`); `python -m uam.self_update` is the helper."""

from uam.self_update.service import SelfStatus, SelfUpdater

__all__ = ["SelfStatus", "SelfUpdater"]
