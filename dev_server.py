"""Local development server for previewing the Carrier Billing Guide Generator."""
import http.server
import json
import os
import socketserver

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

            # If key is set, use the real scrape logic
            try:
                import anthropic
                import requests
                from bs4 import BeautifulSoup
                import re

                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                }
                resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
                resp.raise_for_status()

                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup.find_all(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
                    tag.decompose()
                main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
                text = (main or soup).get_text(separator="\n", strip=True)
                text = re.sub(r"\n{3,}", "\n\n", text)
                if len(text) > 12000:
                    text = text[:12000] + "\n\n[Content truncated...]"

                system_prompt = """You are a technical writer at Zonos, a cross-border e-commerce company.
Your job is to assess shipping platforms and create setup guides for the onboarding team.

You must FIRST assess whether the platform supports BOTH of these capabilities:

1. **Third-Party Billing for Duties & Taxes ONLY** — The ability to bill duties and taxes to a separate third-party account (the Zonos DDP account) WITHOUT also billing shipping charges to that account. The platform must have a dedicated field or option for billing D&T independently from shipping. If the platform only supports billing both shipping and D&T together to a third party (no separation), this does NOT qualify.

2. **Adding VAT/Tax IDs** — The ability to add VAT, EORI, IOSS, or other tax identification numbers so they appear on commercial invoices and customs documents.

IMPORTANT: You must respond in one of two ways:

**If EITHER capability is NOT supported**, respond with EXACTLY this JSON format and nothing else:
{"supported": false, "platform": "<platform name>", "missing": "<brief explanation of what is not supported>"}

**If BOTH capabilities ARE supported**, respond with EXACTLY this JSON format:
{"supported": true, "platform": "<platform name>", "guide": "<full markdown guide>"}

Guide rules (only if supported):
- Write numbered step-by-step instructions.
- Use markdown formatting with # for main title, ## for sections, ### for subsections.
- Keep instructions concise and actionable.
- Include any relevant field names, menu paths, or settings exactly as they appear in the source.
- Add a "Notes" subsection if there are important caveats or limitations.
- Start with a # title that names the platform."""

                client = anthropic.Anthropic(api_key=api_key)
                message = client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": f"URL: {url}\n\n{text}\n\nFirst assess if the platform supports BOTH separate D&T third-party billing AND tax IDs, then respond with the appropriate JSON format."}],
                )

                raw = message.content[0].text
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    self._json_response(200, {"guide": raw, "source_url": url, "supported": True})
                    return

                if result.get("supported"):
                    self._json_response(200, {
                        "guide": result.get("guide", ""),
                        "source_url": url,
                        "supported": True,
                        "platform": result.get("platform", ""),
                    })
                else:
                    self._json_response(200, {
                        "supported": False,
                        "source_url": url,
                        "platform": result.get("platform", ""),
                        "missing": result.get("missing", "Required features are not supported by this platform."),
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
