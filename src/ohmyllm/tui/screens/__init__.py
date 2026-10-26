"""TUI screens."""

from .backend import BackendScreen
from .chat import ChatScreen
from .chats import ChatsScreen
from .doctor import DoctorScreen
from .help import HelpScreen
from .market import MarketScreen
from .memory import MemoryScreen
from .models import ModelsScreen
from .perms import PermsScreen
from .providers import ProvidersScreen
from .settings import SettingsScreen
from .wizard import WizardScreen

SCREENS: dict[str, type] = {
    "market": MarketScreen,
    "chat": ChatScreen,
    "chats": ChatsScreen,
    "models": ModelsScreen,
    "backend": BackendScreen,
    "perms": PermsScreen,
    "providers": ProvidersScreen,
    "memory": MemoryScreen,
    "settings": SettingsScreen,
    "doctor": DoctorScreen,
    "help": HelpScreen,
    "wizard": WizardScreen,
}

__all__ = ["SCREENS"]
