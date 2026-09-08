from __future__ import annotations

from .consumer_shell import ConsumerShell


_DEFER_LAUNCH_FLAG = "_profile_create_redirect_defer_launch"


def install_profile_create_redirect() -> None:
    """Keep the first-profile POST response bounded to durable creation.

    The existing profile-create handler creates canonical local Artist state and
    then immediately opens the same Headquarters runtime before returning its
    303. On slower Windows runners that second SQLite composition can outlive
    the browser's response deadline even though profile creation itself already
    succeeded.

    Preserve the existing handler and authority checks, but defer only that
    immediate runtime launch. The redirected GET follows the normal
    ``_ensure_runtime`` path, reacquires the exact per-profile runtime lease and
    opens Headquarters before rendering any running state.
    """

    if getattr(ConsumerShell, "_profile_create_redirect_installed", False):
        return

    original_post_create = ConsumerShell._post_create_profile
    original_launch_profile = ConsumerShell._launch_profile

    def _post_create_profile(shell, handler, form) -> None:  # noqa: ANN001
        prior = bool(getattr(shell, _DEFER_LAUNCH_FLAG, False))
        setattr(shell, _DEFER_LAUNCH_FLAG, True)
        try:
            original_post_create(shell, handler, form)
        finally:
            setattr(shell, _DEFER_LAUNCH_FLAG, prior)

    def _launch_profile(shell, profile_id):  # noqa: ANN001
        if bool(getattr(shell, _DEFER_LAUNCH_FLAG, False)):
            return None
        return original_launch_profile(shell, profile_id)

    ConsumerShell._post_create_profile = _post_create_profile  # type: ignore[method-assign]
    ConsumerShell._launch_profile = _launch_profile  # type: ignore[method-assign]
    ConsumerShell._profile_create_redirect_installed = True  # type: ignore[attr-defined]
