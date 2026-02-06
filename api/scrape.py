import json
import os
import re
from http.server import BaseHTTPRequestHandler

import anthropic
import requests
from bs4 import BeautifulSoup

SYSTEM_PROMPT = """You are a technical writer at Zonos, a cross-border e-commerce company.
Your job is to create clear, step-by-step guides for the onboarding team.

Given the content of a shipping platform's help/docs page, generate a guide covering:

1. **Third-Party Billing for Duties & Taxes** — How to set up label creation so that duties and taxes are billed to a third-party account (the Zonos DDP account) rather than the shipper or receiver.

2. **Adding VAT Tax IDs** — How to add VAT/tax identification numbers in the platform so they appear on commercial invoices and customs documents.

Rules:
- Write numbered step-by-step instructions.
- If the page content clearly covers one of the topics, provide detailed steps.
- If the page content does NOT cover one of the topics, say "Not found in this page" and suggest what to search for or where to look.
- Use markdown formatting with # for main title, ## for sections, ### for subsections.
- Keep instructions concise and actionable.
- Include any relevant field names, menu paths, or settings exactly as they appear in the source.
- Add a "Notes" subsection if there are important caveats or limitations.
- Start with a # title that names the platform (e.g., "# ShipHero: Carrier Billing & VAT Setup Guide").
"""

USER_PROMPT_TEMPLATE = """Here is the content from the shipping platform's documentation page.

URL: {url}

--- PAGE CONTENT ---
{content}
--- END PAGE CONTENT ---

Please generate the carrier billing guide based on this content."""


def fetch_page(url: str) -> str:
    """Fetch a URL and extract readable text content."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove non-content elements
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
        tag.decompose()

    # Try to find main content area
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    # Clean up excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Truncate to ~12000 chars to stay within reasonable token limits
    if len(text) > 12000:
        text = text[:12000] + "\n\n[Content truncated...]"

    return text


def generate_guide(url: str, content: str) -> str:
    """Send page content to Claude and get a formatted guide."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(url=url, content=content),
            }
        ],
    )

    return message.content[0].text


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            url = data.get("url", "").strip()
            if not url:
                self._send_json(400, {"error": "Missing 'url' in request body"})
                return

            # Basic URL validation
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            # Fetch page
            try:
                page_content = fetch_page(url)
            except requests.exceptions.Timeout:
                self._send_json(504, {"error": "Timed out fetching the page. Please check the URL."})
                return
            except requests.exceptions.RequestException as e:
                self._send_json(502, {"error": f"Could not fetch the page: {str(e)}"})
                return

            if len(page_content.strip()) < 50:
                self._send_json(422, {"error": "The page returned very little text content. It may require JavaScript to render. Try a direct documentation link."})
                return

            # Generate guide
            try:
                guide = generate_guide(url, page_content)
            except anthropic.APIError as e:
                self._send_json(502, {"error": f"AI service error: {str(e)}"})
                return

            self._send_json(200, {"guide": guide, "source_url": url})

        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON in request body"})
        except Exception as e:
            self._send_json(500, {"error": f"Internal error: {str(e)}"})

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
