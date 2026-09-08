"""Restore catalog preferences without treating past calls as user selections."""

from pathlib import Path

from harness.catalog import Catalog, ResolvedModel, UnknownAliasError, UnknownBackendError
from harness.events import ModelSelected
from harness.fold import fold
from harness.log import SessionLock, read_session
from harness.types import SessionId


class ModelSelectionError(ValueError):
    pass


def read_model_selection(base: Path, session_id: SessionId) -> ModelSelected | None:
    """Preflight a closed session, with the same ownership/repair rules as resume.

    The kernel replays again under its writer lock before applying the choice.
    Legacy logs lack pin intent; do not guess it from default_model or calls.
    """
    with SessionLock(base, session_id, recover_stale=True) as lock:
        return fold(read_session(base, session_id, repair=True, _lock=lock)).model_selection


def load_selected_catalog(
    selection: ModelSelected, path: Path | None,
) -> tuple[Catalog, ResolvedModel]:
    """Validate against current configuration, never saved endpoints or credentials."""
    remedy = "Restore the alias in --catalog, or resume with --model <available-alias>."
    try:
        if path is None:
            raise FileNotFoundError
        catalog = Catalog.load(path)
        resolved = catalog.resolve(str(selection.model))
    except UnknownAliasError:
        raise ModelSelectionError(
            f"saved model {selection.model!r} is absent from the catalog. {remedy}"
        ) from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnknownBackendError) as exc:
        # Catalog errors can include raw TOML/endpoint text; don't expose it here.
        raise ModelSelectionError(
            f"saved model {selection.model!r} cannot be configured ({type(exc).__name__}). {remedy}"
        ) from None
    return catalog, resolved
