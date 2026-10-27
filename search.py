"""Compatibility entrypoint for `python search.py --port 8083` and the search check suite."""
import importlib

_MODULES = ('studio.search.config', 'studio.search.chunking', 'studio.search.crawler', 'studio.search.store', 'studio.search.ranking', 'studio.search.extractors', 'studio.search.embedder', 'studio.search.indexer', 'studio.search.preview', 'studio.search.api', 'studio.search.cli')

def _owner_module(name):
    for module in _MODULES:
        owner = importlib.import_module(module)
        if name in vars(owner):
            return owner
    return None


def __getattr__(name):
    owner = _owner_module(name)
    if owner is not None:
        return getattr(owner, name)
    raise AttributeError(name)


if __name__ == '__main__':
    from studio.search.cli import main
    main()
