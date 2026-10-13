"""E2E tests for click coordinate precision."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests


@pytest.fixture(scope="module")
def precision_server_url():
    """A tiny page with a wide link whose right-hand side overlaps a dummy icon.

    The link covers the full width, but the centre of its bounding box lands
    on the icon.  The recommended left-inset click point should still hit the
    link and navigate.
    """
    html = b"""<html><head><style>
        #container { position: relative; width: 400px; height: 60px; }
        a#target { display: block; width: 400px; height: 60px; background: #ccf; }
        #icon { position: absolute; right: 0; top: 0; width: 40px; height: 60px; background: #fcc; pointer-events: none; }
    </style></head><body>
    <div id="container">
        <a id="target" href="/landed">Navigate here</a>
        <div id="icon">icon</div>
    </div>
    <p id="result"></p>
    </body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/landed":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Landed</h1></body></html>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args, **kwargs):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield url
    finally:
        server.shutdown()


@pytest.mark.smoke
def test_find_element_returns_left_inset_click_point(base_url, precision_server_url):
    """find_element returns a click point inset from the left, not the centre."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": precision_server_url, "return_look": False},
        timeout=30,
    ).raise_for_status()

    response = requests.post(
        f"{base_url}/api/find_element",
        json={"text": "Navigate here", "element_type": "link"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    assert data["found"] is True
    # The element is 400 px wide starting at x=0.  Centre would be ~x=200;
    # the recommended point should be left-inset (<= ~10 px from left edge).
    assert data["x"] < 200, f"expected left-inset x, got {data['x']}"
    # It must still be inside the element.
    assert data["x"] >= 0


@pytest.mark.smoke
def test_click_on_inset_point_navigates(base_url, precision_server_url):
    """Clicking the recommended left-inset point navigates even when the centre would hit an icon."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": precision_server_url, "return_look": False},
        timeout=30,
    ).raise_for_status()

    found = requests.post(
        f"{base_url}/api/find_element",
        json={"text": "Navigate here", "element_type": "link"},
        timeout=10,
    ).json()
    assert found["found"] is True

    response = requests.post(
        f"{base_url}/api/click",
        json={"x": found["x"], "y": found["y"], "return_look": False},
        timeout=10,
    )
    response.raise_for_status()

    url_response = requests.get(f"{base_url}/api/url", timeout=10)
    url_response.raise_for_status()
    assert "/landed" in url_response.json()["url"]
