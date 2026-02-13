"""Local development server for previewing the Carrier Billing Guide Generator."""
import http.server
import json
import os
import socketserver

# Load .env file if it exists
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")

PORT = 3000
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")

class DevHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_POST(self):
        if self.path == "/api/scrape":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            url = data.get("url", "")

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                self._json_response(500, {
                    "error": "ANTHROPIC_API_KEY not set. Export it in your terminal: export ANTHROPIC_API_KEY=sk-ant-..."
                })
                return

            # If key is set, use the real scrape logic from api/scrape.py
            try:
                import sys
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from api.scrape import fetch_page, generate_guide, _parse_ai_json

                result = fetch_page(url)
                page_content = result["content"]
                used_js = result["used_js"]

                if len(page_content.strip()) < 50:
                    self._json_response(422, {
                        "error": "The page returned very little text content. This can happen with heavily JS-rendered pages. "
                                 "Try a more specific documentation page URL, or a page that has the setup instructions directly."
                    })
                    return

                raw = generate_guide(url, page_content)
                try:
                    ai_result = _parse_ai_json(raw)
                except json.JSONDecodeError:
                    self._json_response(200, {"guide": raw, "source_url": url, "supported": True, "used_js": used_js})
                    return

                if ai_result.get("supported"):
                    self._json_response(200, {
                        "guide": ai_result.get("guide", ""),
                        "source_url": url,
                        "supported": True,
                        "platform": ai_result.get("platform", ""),
                        "used_js": used_js,
                    })
                else:
                    self._json_response(200, {
                        "supported": False,
                        "source_url": url,
                        "platform": ai_result.get("platform", ""),
                        "missing": ai_result.get("missing", "Required features are not supported by this platform."),
                        "used_js": used_js,
                    })
            except Exception as e:
                self._json_response(500, {"error": str(e)})
        else:
            self.send_error(404)

    def _json_response(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

with socketserver.TCPServer(("", PORT), DevHandler) as httpd:
    print(f"Carrier Billing Guide running at http://localhost:{PORT}")
    httpd.serve_forever()
