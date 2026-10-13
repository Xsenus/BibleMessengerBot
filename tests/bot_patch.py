"""Patch a helper in every Telegram bot module that references it.

Handler logic is split across app.bot.{handlers,dispatch,menus,common}; a name imported into
several of them must be replaced everywhere for a test double to take effect.
"""
from app.bot import common, dispatch, handlers, menus


def patch_bot(monkeypatch, name, value):
    found = False
    for module in (handlers, dispatch, menus, common):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            found = True
    assert found, name
