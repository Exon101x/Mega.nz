"""If Telethon is not installed (e.g. in CI), install lightweight stand-ins so the logic can still be tested."""
import sys
import types as pytypes


def install_if_missing():
    try:
        import telethon  # noqa: F401
        return False
    except ImportError:
        pass

    def make(name):
        m = pytypes.ModuleType(name)

        def __getattr__(attr):
            if attr.startswith("__"):
                raise AttributeError(attr)
            if attr.endswith("Error"):
                cls = type(attr, (Exception,), {"seconds": 1})
            else:
                cls = type(attr, (), {"__init__": lambda self, *a, **k: None})
            setattr(m, attr, cls)
            return cls

        m.__getattr__ = __getattr__
        sys.modules[name] = m
        return m

    root = make("telethon")
    errors = make("telethon.errors")
    utils = make("telethon.utils")
    events = make("telethon.events")
    sessions = make("telethon.sessions")
    tl = make("telethon.tl")
    functions = make("telethon.tl.functions")
    types = make("telethon.tl.types")
    root.errors, root.utils, root.events, root.sessions, root.tl = errors, utils, events, sessions, tl
    tl.functions, tl.types = functions, types

    class Button:
        @staticmethod
        def inline(text, data):
            return (text, data)

    root.Button = Button
    return True
