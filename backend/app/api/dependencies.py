"""Shared FastAPI dependencies."""

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config import Settings


def get_app_settings(request: Request) -> Settings:
    """Return the settings the running app was built with.

    Reading from ``app.state`` (instead of the global ``get_settings()``) lets
    tests build an app with their own settings without patching globals.
    """
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
