import sys, json
for line in sys.stdin:
    req = json.loads(line)
    mid = req.get('id')
    m = req.get('method')
    if m == 'initialize':
        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'echo', 'version': '1'}}}
    elif m == 'tools/list':
        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'ping', 'description': 'p', 'inputSchema': {'type': 'object'}}]}}
    elif m == 'tools/call':
        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'pong'}]}}
    sys.stdout.write(json.dumps(resp) + chr(10))
    sys.stdout.flush()
