"""Independent search-service HTTP and patch ownership contracts (no model needed)."""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
TEST_STATE = tempfile.TemporaryDirectory(prefix="ls-search-contract-", dir="/tmp")
os.environ["LOCAL_STUDIO_HOME"] = TEST_STATE.name
import search
from fastapi import FastAPI

# The search wrapper closes over its own FastAPI app. Inspect that actual app, not a source approximation.
wrapper = search.make_app(None, None, None, None)
api = next(cell.cell_contents for cell in wrapper.__closure__ if isinstance(cell.cell_contents, FastAPI))
routes = [{'methods': sorted(route.methods), 'path': route.path, 'name': route.name} for route in api.routes]
fixture = REPO / 'macos/fixtures/search_routes.json'
if os.environ.get('LOCAL_STUDIO_UPDATE_FIXTURES') == '1':
    fixture.write_text(json.dumps(routes, indent=1) + '\n')
else:
    assert routes == json.loads(fixture.read_text()), 'Search HTTP routes changed'
print('   ok search_http_contract')

for name, module in {'extract': 'extractors', 'HOME': 'config', 'DOCUMENT_READER': 'config',
                     'EXTRACT_VERSION': 'store', 'REDO_KINDS': 'store', '_dataless': 'crawler'}.items():
    owner = importlib.import_module('studio.search.' + module)
    assert search._owner_module(name) is owner, (name, search._owner_module(name))
    assert getattr(search, name) is getattr(owner, name)
print('   ok search_patch_owners_and_live_reexports')


from fastapi.testclient import TestClient
import types
indexer = types.SimpleNamespace(config=lambda: {'incremental': True}, full_indexing=True, error=None)
searcher = types.SimpleNamespace(search=lambda *a: [], passages=lambda *a: [])
c = TestClient(search.make_app(None, None, indexer, searcher), base_url='http://127.0.0.1:8083')
for endpoint in ('/search', '/passages'):
    result = c.post(endpoint, json={'query': 'synthetic'})
    assert result.status_code == 503 and result.headers['retry-after'] == '1', result.text
indexer.full_indexing = False
assert c.post('/search', json={'query':'synthetic'}).status_code == 200
indexer.full_indexing = True; indexer.config = lambda: {'incremental': False}
assert c.post('/search', json={'query':'synthetic'}).status_code == 200
print('   ok search_full_index_retry_after_and_default_behavior')

# A real full pass must release HTTP backpressure while wait_clear holds it,
# and reinstate it when indexing resumes. No model or user's folders are used.
import threading
import time
for reason in ('user', 'battery', 'busy'):
    cfg = {'incremental': True, 'roots': [], 'removed': [],
           'paused': reason == 'user', 'on_battery': False}
    store = types.SimpleNamespace(get_meta=lambda key: None, sync=lambda *a, **kw: None,
                                  pending=lambda: [{'id': 1, 'path': '/synthetic/pending.txt'}])
    ix = search.Indexer(store, None, config=lambda: cfg, app_url=None)
    ix.on_ac = lambda: reason != 'battery'
    ix.app_busy = lambda: reason == 'busy'
    indexing, release = threading.Event(), threading.Event()
    def index_file(f):
        indexing.set()
        assert release.wait(5), 'test did not release synthetic indexing'
        return 'ok'
    ix.index_file = index_file
    client = TestClient(search.make_app(None, None, ix, searcher), base_url='http://127.0.0.1:8083')
    worker = threading.Thread(target=ix.run_pass)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while ix.state != 'paused' and time.monotonic() < deadline:
            time.sleep(.01)
        assert ix.state == 'paused' and ix.paused_reason == reason
        assert not ix.full_indexing
        for endpoint, key in (('/search', 'results'), ('/passages', 'passages')):
            result = client.post(endpoint, json={'query': 'already indexed'})
            assert result.status_code == 200 and result.json() == {key: []}, result.text
        cfg['paused'] = False
        ix.on_ac = lambda: True
        ix.app_busy = lambda: False
        assert indexing.wait(5), 'full pass did not resume'
        for endpoint in ('/search', '/passages'):
            result = client.post(endpoint, json={'query': 'synthetic'})
            assert result.status_code == 503 and result.headers['retry-after'] == '1', result.text
    finally:
        release.set()
        ix.stop.set()
        worker.join(5)
    assert not worker.is_alive() and not ix.full_indexing and ix.problem is None
    for endpoint in ('/search', '/passages'):
        assert client.post(endpoint, json={'query': 'synthetic'}).status_code == 200
    print(f'   ok search_full_pass_{reason}_pause_allows_queries_and_resume_restores_backpressure')

state = Path(TEST_STATE.name)
(state/'data').mkdir()
(state/'data/settings.json').write_text(json.dumps({'security':{'api_token':True}}))
(state/'data/.api-token').write_text('synthetic-token')
assert c.get('/health').status_code == 200
assert c.get('/status').status_code == 401
assert c.post('/search',json={'query':'test'}).status_code == 401
assert c.post('/search',json={'query':'test'},headers={'Authorization':'Bearer synthetic-token'}).status_code == 200
assert c.post('/search',json={'query':'test'},headers={'Authorization':'Bearer synthetic-token','origin':'https://evil.example'}).status_code == 403
print('   ok search_token_authentication_preserves_localonly')
TEST_STATE.cleanup()


# Rebound search names must be read through owners, just like the UI backend's P1 rule.
import ast
patched = {'extract','HOME','DOCUMENT_READER','EXTRACT_VERSION','REDO_KINDS','_dataless'}
for path in (REPO/'studio/search').glob('*.py'):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node,ast.ImportFrom) and (node.module or '').startswith('studio.search'):
            assert not any(alias.name in patched for alias in node.names), (path,node.lineno)
import patch_guard
patch_guard.install(search)
try:
    search.extract = None
    raise AssertionError('stale search re-export patch was accepted')
except AttributeError:
    pass
print('   ok search_rebound_import_rule_and_patch_guard')
