import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# 25 fake records so you can verify pagination works
FAKE_RECORDS = [
    {"id": i, "name": f"Record {i}", "amount": round(i * 19.99, 2), "status": "active"}
    for i in range(1, 26)
]

class MockERPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        limit = int(params.get("limit", [500])[0])
        offset = int(params.get("offset", [0])[0])

        page = FAKE_RECORDS[offset : offset + limit]
        payload = {
            "data": page,
            "meta": {"total": len(FAKE_RECORDS), "limit": limit, "offset": offset},
        }

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    print("Mock ERP running on http://localhost:8001")
    HTTPServer(("127.0.0.1", 8001), MockERPHandler).serve_forever()