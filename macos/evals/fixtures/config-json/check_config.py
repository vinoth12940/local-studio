import json

with open('settings.json') as stream:
    config = json.load(stream)
assert config['port'] == 8080 and config['name'] == 'demo'
print('config ok')
