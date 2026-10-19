"""Patch guard for the check suites: `app.X = fake` must reach the code that reads X.

While app.py holds all the code, every name a suite patches lives in app.py and this guard does nothing. Once code moves to
studio/, app.py only forwards names (PEP 562 `__getattr__`), and a suite that still assigns `app.X` would set a copy that
nothing reads: the check passes while testing nothing. The guard turns that into an error naming the module to patch instead
(see macos/fixtures/patch_points.json for each name's destination).
"""
import sys
import types


def _studio_owner(name, value):
    """The studio.* module that defines `name` (as the same object), if any."""
    for modname, mod in list(sys.modules.items()):
        if (modname == "studio" or modname.startswith("studio.")) and mod is not None:
            if name in vars(mod) and vars(mod)[name] is value:
                return modname
    return None


class _GuardedModule(types.ModuleType):
    def __setattr__(self, name, value):
        own = vars(self)
        if name not in own:
            raise AttributeError(f"app.{name} is not defined in app.py: patching it here would change nothing the code reads. "
                                 f"Patch the module that owns it (macos/fixtures/patch_points.json).")
        owner = _studio_owner(name, own[name])
        if owner is not None:
            raise AttributeError(f"app.{name} is a re-export of {owner}.{name}: patch {owner}.{name} instead, or the code under "
                                 f"test never sees the fake.")
        super().__setattr__(name, value)


def install(module):
    module.__class__ = _GuardedModule
    return module


def owner(module, name):
    """The module a patch of `module.name` has to go to: the one that defines it (for the suites' setattr save/restore loops)."""
    own = vars(module)
    if name in own:
        found = _studio_owner(name, own[name])
        return sys.modules[found] if found else module
    find = own.get("_owner_module")                     # app.py after the split: the studio module that defines `name`
    return (find(name) if find else None) or module
