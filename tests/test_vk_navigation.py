"""Every visible shared navigation button is accepted by the VK dispatcher."""
import json

import pytest

from app.bot.ui import keyboard_command, main_keyboard
from app.vkbot.dispatcher import NAVIGATION
from app.vkbot.ui import keyboard


@pytest.mark.parametrize('locale', ['ru', 'en'])
def test_visible_vk_navigation_is_dispatchable(locale):
    markup = json.loads(keyboard(main_keyboard(locale, collapsible=False)))
    commands = {keyboard_command(button['action']['label'])
                for row in markup['buttons'] for button in row}
    assert '/prayerbook' in commands
    assert commands <= NAVIGATION
    assert '/admin' not in NAVIGATION and '/reset' not in NAVIGATION
