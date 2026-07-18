import os, urllib.request, json, sys
url = 'http://localhost:8000/config/apply'
body = json.dumps({"evidence_gate_enabled": True}).encode()
key = os.environ.get('DASHBOARD_API_KEY', '')
headers = {'Content-Type': 'application/json'}
if key:
    headers['X-API-Key'] = key
req = urllib.request.Request(url, data=body, headers=headers, method='POST')
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.read().decode())
except Exception as e:
    print('ERROR', e)
