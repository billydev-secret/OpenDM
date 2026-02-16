import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_fake_dotenv():
    dotenv_mod = ModuleType("dotenv")
    dotenv_mod.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv_mod


def _install_fake_discord():
    discord_mod = ModuleType("discord")

    class Forbidden(Exception):
        pass

    class NotFound(Exception):
        pass

    class Intents:
        @staticmethod
        def default():
            return SimpleNamespace(members=False)

    class Object:
        def __init__(self, id):
            self.id = id

    class Embed:
        def __init__(self, title=None, description=None, color=None):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []
            self.footer = None
            self.thumbnail = None
            self.author = None

        def add_field(self, name, value, inline=False):
            self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))

        def set_footer(self, text):
            self.footer = text

        def set_thumbnail(self, url):
            self.thumbnail = url

        def set_author(self, name, icon_url):
            self.author = SimpleNamespace(name=name, icon_url=icon_url)

    class Color:
        @staticmethod
        def gold():
            return 1

        @staticmethod
        def green():
            return 2

        @staticmethod
        def red():
            return 3

        @staticmethod
        def orange():
            return 4

        @staticmethod
        def blurple():
            return 5

    class Client:
        def __init__(self, intents=None):
            self.intents = intents
            self.user = SimpleNamespace(id=1)

        def event(self, func):
            return func

        def run(self, token):
            return None

    class View:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = []

    def button(label=None, style=None):
        def decorator(func):
            return func

        return decorator

    class ButtonStyle:
        success = 1
        danger = 2

    def utils_get(iterable, **attrs):
        name = attrs.get("name")
        for item in iterable:
            if getattr(item, "name", None) == name:
                return item
        return None

    discord_mod.Forbidden = Forbidden
    discord_mod.NotFound = NotFound
    discord_mod.Intents = Intents
    discord_mod.Object = Object
    discord_mod.Embed = Embed
    discord_mod.Color = Color
    discord_mod.Client = Client
    discord_mod.utils = SimpleNamespace(get=utils_get)
    discord_mod.ui = SimpleNamespace(View=View, button=button, Button=object)
    discord_mod.ButtonStyle = ButtonStyle

    # type placeholders used only in annotations
    discord_mod.Member = object
    discord_mod.Guild = object
    discord_mod.Interaction = object
    discord_mod.TextChannel = object

    app_commands_mod = ModuleType("app_commands")

    class CommandTree:
        def __init__(self, client):
            self.client = client
            self._commands = []

        async def sync(self, guild=None):
            return None

        def command(self, name, description, guild=None):
            def decorator(func):
                self._commands.append(
                    SimpleNamespace(name=name, description=description, guild=guild, callback=func)
                )
                return func

            return decorator

        def get_commands(self):
            return list(self._commands)

    class Choice:
        def __init__(self, name, value):
            self.name = name
            self.value = value

        @classmethod
        def __class_getitem__(cls, item):
            return cls

    def describe(**kwargs):
        def decorator(func):
            return func

        return decorator

    def choices(**kwargs):
        def decorator(func):
            return func

        return decorator

    app_commands_mod.CommandTree = CommandTree
    app_commands_mod.Choice = Choice
    app_commands_mod.describe = describe
    app_commands_mod.choices = choices

    discord_mod.app_commands = app_commands_mod

    sys.modules["discord"] = discord_mod
    sys.modules["discord.app_commands"] = app_commands_mod


@pytest.fixture
def accord_module():
    _install_fake_dotenv()
    _install_fake_discord()

    if "accord" in sys.modules:
        module = importlib.reload(sys.modules["accord"])
    else:
        module = importlib.import_module("accord")

    module.INTERACTION_PAIRS = {}
    module.REQUEST_CHANNELS = {}
    module.AUDIT_LOG_CHANNEL_ID = None
    return module
