import json
import os
import re
from http.server import BaseHTTPRequestHandler

import anthropic
import requests
from bs4 import BeautifulSoup
from scrapingbee import ScrapingBeeClient

SYSTEM_PROMPT = """You are a technical writer at Zonos, a cross-border e-commerce company.
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
- Start with a # title that names the platform (e.g., "# ShipHero: Carrier Billing & VAT Setup Guide").
"""

USER_PROMPT_TEMPLATE = """Here is the content from the shipping platform's documentation page.

URL: {url}

--- PAGE CONTENT ---
{content}
--- END PAGE CONTENT ---

First assess if the platform supports BOTH separate D&T third-party billing AND tax IDs, then respond with the appropriate JSON format."""


def _extract_text(html: str) -> str:
    """Parse HTML and extract readable text content."""
    soup = BeautifulSoup(html, "html.parser")

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


def _traverse_json(obj, depth=0) -> str:
    """Recursively traverse a JSON structure and extract text values."""
    if depth > 15:
        return ""

    if isinstance(obj, str):
        stripped = obj.strip()
        # Skip URLs, CSS, base64, very short strings, and code-like values
        if (
            len(stripped) < 30
            or stripped.startswith(("http://", "https://", "/", "data:", "{", "[", "/*", "//"))
            or re.match(r"^[a-f0-9\-]{20,}$", stripped)  # UUIDs / hashes
            or re.match(r"^[\w./\-]+\.(js|css|png|jpg|svg|woff|ico)$", stripped)  # file paths
        ):
            return ""
        # If the string contains HTML, extract text from it
        if "<" in stripped and ">" in stripped:
            inner_soup = BeautifulSoup(stripped, "html.parser")
            return inner_soup.get_text(separator="\n", strip=True)
        return stripped

    if isinstance(obj, list):
        parts = [_traverse_json(item, depth + 1) for item in obj]
        return "\n".join(p for p in parts if p)

    if isinstance(obj, dict):
        # Prioritize content-related keys
        priority_keys = [
            "content", "body", "text", "description", "html", "markdown",
            "raw", "title", "heading", "paragraph", "excerpt", "summary",
            "articleBody", "mainEntity", "pageContent", "renderedContent",
        ]
        results = []

        # First pass: priority keys
        for key in priority_keys:
            if key in obj:
                val = _traverse_json(obj[key], depth + 1)
                if val:
                    results.append(val)

        # Second pass: other keys (skip metadata-like keys)
        skip_keys = {
            "id", "url", "href", "src", "slug", "path", "route",
            "buildId", "assetPrefix", "scriptLoader", "locale", "locales",
            "defaultLocale", "domainLocales", "isPreview", "runtimeConfig",
            "gssp", "gip", "appGip", "isFallback", "dynamicIds",
            "customServer", "css", "className", "style", "image",
            "icon", "logo", "favicon", "og", "twitter", "meta",
        }
        for key, val in obj.items():
            if key in priority_keys or key in skip_keys:
                continue
            extracted = _traverse_json(val, depth + 1)
            if extracted:
                results.append(extracted)

        return "\n".join(results)

    return ""


def _extract_json_content(html: str) -> str:
    """Extract text content from embedded JSON in script tags.

    Handles Next.js (__NEXT_DATA__), Nuxt (__NUXT__), Gatsby,
    and generic application/json script tags.
    """
    soup = BeautifulSoup(html, "html.parser")
    texts = []

    # 1. Next.js __NEXT_DATA__
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            data = json.loads(next_data.string)
            # Focus on pageProps which holds actual page content
            page_props = data.get("props", {}).get("pageProps", data)
            extracted = _traverse_json(page_props)
            if extracted.strip():
                texts.append(extracted)
        except (json.JSONDecodeError, AttributeError):
            pass

    # 2. Nuxt.js __NUXT__ / __NUXT_DATA__
    for script in soup.find_all("script"):
        if not script.string:
            continue
        if "__NUXT__" in script.string or "__NUXT_DATA__" in script.string:
            match = re.search(r'__NUXT(?:_DATA)?__\s*=\s*(\{.+\})', script.string, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    extracted = _traverse_json(data)
                    if extracted.strip():
                        texts.append(extracted)
                except json.JSONDecodeError:
                    pass

    # 3. Generic application/json script tags (Gatsby, custom frameworks)
    for script in soup.find_all("script", type="application/json"):
        if script.string and script.get("id") != "__NEXT_DATA__":
            try:
                data = json.loads(script.string)
                extracted = _traverse_json(data)
                if extracted.strip():
                    texts.append(extracted)
            except json.JSONDecodeError:
                pass

    # 4. JSON-LD structured data (can contain article body, descriptions)
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                data = json.loads(script.string)
                extracted = _traverse_json(data)
                if extracted.strip():
                    texts.append(extracted)
            except json.JSONDecodeError:
                pass

    combined = "\n\n".join(t for t in texts if t.strip())

    # Clean up and truncate
    combined = re.sub(r"\n{3,}", "\n\n", combined)
    if len(combined) > 12000:
        combined = combined[:12000] + "\n\n[Content truncated...]"

    return combined


def _extract_openapi_content(html: str, base_url: str) -> str:
    """Detect OpenAPI/Swagger/ReDoc pages and extract relevant API documentation.

    Looks for spec-url attributes (ReDoc), swagger-ui configurations, or
    direct links to .json/.yaml spec files. Fetches the spec and extracts
    relevant content about billing, shipping, tax IDs, and carrier configuration.
    """
    soup = BeautifulSoup(html, "html.parser")
    spec_url = None

    # 1. ReDoc: <redoc spec-url="...">
    redoc = soup.find("redoc")
    if redoc and redoc.get("spec-url"):
        spec_url = redoc["spec-url"]

    # 2. Swagger UI: look for url in SwaggerUIBundle config
    if not spec_url:
        for script in soup.find_all("script"):
            if script.string and "SwaggerUIBundle" in script.string:
                match = re.search(r'url\s*:\s*["\']([^"\']+)["\']', script.string)
                if match:
                    spec_url = match.group(1)
                    break

    # 3. Direct link to spec file in any script tag
    if not spec_url:
        for script in soup.find_all("script"):
            if script.string:
                match = re.search(
                    r'["\']([^"\']*(?:swagger|openapi|api-docs)[^"\']*\.(?:json|yaml|yml))["\']',
                    script.string, re.IGNORECASE
                )
                if match:
                    spec_url = match.group(1)
                    break

    # 4. Link tags or meta tags pointing to spec
    if not spec_url:
        for link in soup.find_all("link", rel="openapi"):
            if link.get("href"):
                spec_url = link["href"]
                break

    if not spec_url:
        return ""

    # Resolve relative URLs
    if spec_url.startswith("/"):
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        spec_url = f"{parsed.scheme}://{parsed.netloc}{spec_url}"
    elif not spec_url.startswith(("http://", "https://")):
        spec_url = base_url.rstrip("/") + "/" + spec_url

    # Fetch the spec
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(spec_url, headers=headers, timeout=20)
        resp.raise_for_status()
        spec = resp.json()
    except Exception:
        return ""

    # Extract relevant content from the OpenAPI spec
    return _summarize_openapi(spec)


def _summarize_openapi(spec: dict) -> str:
    """Extract relevant sections from an OpenAPI spec for guide generation.

    Focuses on endpoints and schemas related to billing, shipping, carriers,
    tax IDs, duties, customs, and third-party accounts.
    """
    parts = []

    # API info
    info = spec.get("info", {})
    if info.get("title"):
        parts.append(f"API: {info['title']} (version {info.get('version', 'unknown')})")
    if info.get("description"):
        desc = info["description"]
        if len(desc) > 500:
            desc = desc[:500] + "..."
        parts.append(f"Description: {desc}")

    # Keywords to match for relevant endpoints/schemas
    keywords = [
        "bill", "billing", "third.?party", "duty", "duties", "tax",
        "vat", "eori", "ioss", "customs", "carrier", "ship", "freight",
        "account", "invoice", "tariff", "hts", "hs.?code", "landed",
        "ddt", "ddp", "dap", "charge", "fee", "cost",
    ]
    keyword_pattern = re.compile("|".join(keywords), re.IGNORECASE)

    # Extract relevant paths
    paths = spec.get("paths", {})
    relevant_paths = []

    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue

        path_relevant = bool(keyword_pattern.search(path))

        for method, details in methods.items():
            if method.startswith("x-") or not isinstance(details, dict):
                continue

            summary = details.get("summary", "")
            description = details.get("description", "")
            tags = details.get("tags", [])
            combined = f"{path} {summary} {description} {' '.join(tags)}"

            if path_relevant or keyword_pattern.search(combined):
                entry = f"\n### {method.upper()} {path}"
                if summary:
                    entry += f"\nSummary: {summary}"
                if description:
                    desc = description[:300] + ("..." if len(description) > 300 else "")
                    entry += f"\nDescription: {desc}"
                if tags:
                    entry += f"\nTags: {', '.join(tags)}"

                # Extract request body schema info
                req_body = details.get("requestBody", {})
                if req_body:
                    content = req_body.get("content", {})
                    for ct, ct_detail in content.items():
                        schema = ct_detail.get("schema", {})
                        schema_text = _describe_schema(schema, spec.get("components", {}).get("schemas", {}))
                        if schema_text:
                            entry += f"\nRequest body: {schema_text}"

                # Extract response schema info
                responses = details.get("responses", {})
                for status, resp_detail in responses.items():
                    if status.startswith("2") and isinstance(resp_detail, dict):
                        resp_content = resp_detail.get("content", {})
                        for ct, ct_detail in resp_content.items():
                            schema = ct_detail.get("schema", {})
                            schema_text = _describe_schema(schema, spec.get("components", {}).get("schemas", {}))
                            if schema_text:
                                entry += f"\nResponse ({status}): {schema_text}"

                relevant_paths.append(entry)

    # Extract relevant schemas/components
    schemas = spec.get("components", {}).get("schemas", {})
    relevant_schemas = []

    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        schema_str = json.dumps(schema)
        if keyword_pattern.search(name) or keyword_pattern.search(schema_str[:500]):
            desc = schema.get("description", "")
            props = schema.get("properties", {})
            prop_names = list(props.keys())

            entry = f"\n### Schema: {name}"
            if desc:
                entry += f"\nDescription: {desc[:300]}"
            if prop_names:
                # Show relevant properties
                relevant_props = [p for p in prop_names if keyword_pattern.search(p)]
                if relevant_props:
                    entry += f"\nRelevant fields: {', '.join(relevant_props)}"
                if len(prop_names) <= 20:
                    entry += f"\nAll fields: {', '.join(prop_names)}"
                else:
                    entry += f"\nFields ({len(prop_names)} total): {', '.join(prop_names[:20])}..."

                # Show details for relevant properties
                for prop_name in relevant_props:
                    prop = props[prop_name]
                    if isinstance(prop, dict):
                        prop_desc = prop.get("description", "")
                        prop_type = prop.get("type", "")
                        prop_enum = prop.get("enum", [])
                        detail = f"  - {prop_name} ({prop_type})"
                        if prop_desc:
                            detail += f": {prop_desc[:200]}"
                        if prop_enum:
                            detail += f" [enum: {', '.join(str(e) for e in prop_enum[:10])}]"
                        entry += f"\n{detail}"

            relevant_schemas.append(entry)

    # Build the output
    if relevant_paths:
        parts.append(f"\n## Relevant API Endpoints ({len(relevant_paths)} found)")
        parts.extend(relevant_paths[:30])  # Cap at 30 endpoints
    else:
        # If no keyword matches, include all endpoint summaries
        parts.append(f"\n## All API Endpoints ({len(paths)} total)")
        for path, methods in list(paths.items())[:50]:
            if isinstance(methods, dict):
                for method, details in methods.items():
                    if isinstance(details, dict) and not method.startswith("x-"):
                        summary = details.get("summary", "")
                        parts.append(f"- {method.upper()} {path}: {summary}")

    if relevant_schemas:
        parts.append(f"\n## Relevant Data Models ({len(relevant_schemas)} found)")
        parts.extend(relevant_schemas[:20])  # Cap at 20 schemas

    result = "\n".join(parts)

    # Truncate
    if len(result) > 12000:
        result = result[:12000] + "\n\n[Content truncated...]"

    return result


def _describe_schema(schema: dict, all_schemas: dict, depth=0) -> str:
    """Produce a short text description of a JSON schema."""
    if depth > 3:
        return ""

    if "$ref" in schema:
        ref = schema["$ref"]
        name = ref.split("/")[-1]
        if depth < 2 and name in all_schemas:
            props = all_schemas[name].get("properties", {})
            if props:
                return f"{name} ({', '.join(list(props.keys())[:8])}{'...' if len(props) > 8 else ''})"
        return name

    schema_type = schema.get("type", "")
    if schema_type == "array":
        items = schema.get("items", {})
        return f"array of {_describe_schema(items, all_schemas, depth + 1)}"
    if schema_type == "object":
        props = schema.get("properties", {})
        if props:
            return f"object ({', '.join(list(props.keys())[:8])})"
        return "object"

    return schema_type


def _fetch_raw(url: str) -> str:
    """Fetch raw HTML from a URL."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _scrapingbee_fetch(url: str, api_key: str) -> str:
    """Fetch a JS-rendered page using ScrapingBee."""
    client = ScrapingBeeClient(api_key=api_key)
    resp = client.get(url, params={"render_js": "true", "timeout": "30000"})
    if resp.status_code != 200:
        raise requests.exceptions.RequestException(
            f"ScrapingBee returned status {resp.status_code}"
        )
    return _extract_text(resp.text)


def fetch_page(url: str) -> dict:
    """Fetch a URL and extract readable text content.

    Returns a dict with 'content' (str) and 'used_js' (bool).
    Strategy:
      1. Static fetch raw HTML
      2. Extract text from HTML
      3. If too little text, check for OpenAPI/Swagger/ReDoc spec
      4. If not an API spec, try extracting from embedded JSON (Next.js, Nuxt, etc.)
      5. If still too little, fall back to ScrapingBee (if configured)
    """
    # Step 1: Fetch raw HTML
    raw_html = _fetch_raw(url)

    # Step 2: Try standard text extraction
    content = _extract_text(raw_html)

    # Step 3: If too little content, try OpenAPI spec extraction
    if len(content.strip()) < 200:
        openapi_content = _extract_openapi_content(raw_html, url)
        if len(openapi_content.strip()) > len(content.strip()):
            content = openapi_content

    # Step 4: If still too little, try embedded JSON extraction
    if len(content.strip()) < 200:
        json_content = _extract_json_content(raw_html)
        if len(json_content.strip()) > len(content.strip()):
            content = json_content

    # Step 5: If still too little, try ScrapingBee
    if len(content.strip()) < 200:
        sb_key = os.environ.get("SCRAPINGBEE_API_KEY")
        if sb_key:
            content = _scrapingbee_fetch(url, sb_key)
            return {"content": content, "used_js": True}

    return {"content": content, "used_js": False}


def _parse_ai_json(raw: str) -> dict:
    """Parse Claude's response as JSON, handling markdown code fences."""
    text = raw.strip()

    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Try parsing directly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting the first JSON object from the text
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("Could not find valid JSON", text, 0)


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
                result = fetch_page(url)
                page_content = result["content"]
                used_js = result["used_js"]
            except requests.exceptions.Timeout:
                self._send_json(504, {"error": "Timed out fetching the page. Please check the URL."})
                return
            except requests.exceptions.RequestException as e:
                self._send_json(502, {"error": f"Could not fetch the page: {str(e)}"})
                return

            if len(page_content.strip()) < 50:
                self._send_json(422, {
                    "error": "The page returned very little text content. This can happen with heavily JS-rendered pages. "
                             "Try a more specific documentation page URL, or a page that has the setup instructions directly."
                })
                return

            # Generate guide
            try:
                raw = generate_guide(url, page_content)
            except anthropic.APIError as e:
                self._send_json(502, {"error": f"AI service error: {str(e)}"})
                return

            # Parse the AI's JSON response
            try:
                ai_result = _parse_ai_json(raw)
            except json.JSONDecodeError:
                # Fallback: treat raw text as a guide
                self._send_json(200, {"guide": raw, "source_url": url, "supported": True, "used_js": used_js})
                return

            if ai_result.get("supported"):
                self._send_json(200, {
                    "guide": ai_result.get("guide", ""),
                    "source_url": url,
                    "supported": True,
                    "platform": ai_result.get("platform", ""),
                    "used_js": used_js,
                })
            else:
                self._send_json(200, {
                    "supported": False,
                    "source_url": url,
                    "platform": ai_result.get("platform", ""),
                    "missing": ai_result.get("missing", "Required features are not supported by this platform."),
                    "used_js": used_js,
                })

        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON in request body"})
        except Exception as e:
            self._send_json(500, {"error": f"Internal error: {str(e)}"})

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
