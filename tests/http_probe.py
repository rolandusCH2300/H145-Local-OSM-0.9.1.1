"""Protocol probe for an already-running local server."""
import json, urllib.parse, urllib.request

url = 'http://127.0.0.1:8765/api/interpreter'
q = '[out:json];(area(60.40,22.20,60.50,22.35)[amenity=hospital]; node(60.40,22.20,60.50,22.35)[amenity=hospital]; area(60.40,22.20,60.50,22.35)[aeroway=helipad]; node(60.40,22.20,60.50,22.35)[aeroway=helipad];); out center;'
body = urllib.parse.urlencode({'data': q}).encode()
req = urllib.request.Request(url, data=body, headers={'Content-Type':'application/x-www-form-urlencoded'})
with urllib.request.urlopen(req, timeout=5) as r:
    payload = json.load(r)
print(json.dumps(payload, indent=2, ensure_ascii=False))
assert payload['version'] == 0.6
assert payload['elements']
print(f"OK: {len(payload['elements'])} elements returned")
