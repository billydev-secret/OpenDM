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

    class HTTPException(Exception):
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
            self.guilds = []

        def event(self, func):
            return func

        def run(self, token):
            return None

        def add_view(self, view):
            return None

    class View:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)
            setattr(item, "view", self)

    class UserSelect:
        def __init__(self, placeholder=None, min_values=1, max_values=1):
            self.placeholder = placeholder
            self.min_values = min_values
            self.max_values = max_values
            self.values = []
            self.view = None

    class Modal:
        def __init__(self, *, title=None):
            self.title = title
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class TextInput:
        def __init__(
            self,
            *,
            label=None,
            required=True,
            max_length=None,
            placeholder=None,
            style=None,
        ):
            self.label = label
            self.required = required
            self.max_length = max_length
            self.placeholder = placeholder
            self.style = style
            self.value = ""

    class TextStyle:
        paragraph = 2

    def button(label=None, style=None, custom_id=None):
        def decorator(func):
            return func

        return decorator

    class ButtonStyle:
        primary = 0
        secondary = 3
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
    discord_mod.HTTPException = HTTPException
    discord_mod.Intents = Intents
    discord_mod.Object = Object
    discord_mod.Embed = Embed
    discord_mod.Color = Color
    discord_mod.Client = Client
    discord_mod.TextStyle = TextStyle
    discord_mod.utils = SimpleNamespace(get=utils_get)
    discord_mod.ui = SimpleNamespace(
        View=View,
        Modal=Modal,
        TextInput=TextInput,
        UserSelect=UserSelect,
        button=button,
        Button=object,
    )
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

        def clear_commands(self, guild=None):
            self._commands = []

        def copy_global_to(self, guild=None):
            pass

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

    class Range:
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
    app_commands_mod.Range = Range()
    app_commands_mod.describe = describe
    app_commands_mod.choices = choices

    discord_mod.app_commands = app_commands_mod

    sys.modules["discord"] = discord_mod
    sys.modules["discord.app_commands"] = app_commands_mod


@pytest.fixture
def accord_module():
    _install_fake_dotenv()
    _install_fake_discord()

    # Reload command modules so each test starts with fresh module-level bindings
    import accord_bot.commands.dm as _dm
    _dm = importlib.reload(_dm)

    import accord_bot.commands.debug as _debug
    _debug = importlib.reload(_debug)

    # Reload bot module to get a fresh Bot instance with an empty command tree
    import accord_bot.bot as _bot_mod
    _bot_mod = importlib.reload(_bot_mod)

    # Register all commands on the fresh bot
    _dm.setup(_bot_mod.bot)
    _debug.setup(_bot_mod.bot)

    # Expose bot on the dm module (used by test_command_registration_includes_all_slash_commands)
    _dm.bot = _bot_mod.bot

    # Expose debug command functions on the dm module for unified accord_module access
    _dm.debug_status_check = _debug.debug_status_check
    _dm.debug_permissions_list = _debug.debug_permissions_list
    _dm.debug_permissions_set = _debug.debug_permissions_set
    _dm.debug_permissions_remove = _debug.debug_permissions_remove

    # Reset all mutable state that commands look up in this module's namespace
    _dm.INTERACTION_PAIRS = {}
    _dm.CONSENT_MESSAGES = {}
    _dm.DM_REQUESTS = {}
    _dm.REQUEST_CHANNELS = {}
    _dm.PANEL_SETTINGS = {}
    _dm.AUDIT_LOG_CHANNEL_ID = None

    return _dm
