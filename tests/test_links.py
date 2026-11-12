"""Cek tautan: setiap href sidebar & HTML menuju file dan view yang ada."""

import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGES = [
    "dashboard.html", "monitoring.html", "inventory.html", "services.html", "alerts.html",
    "data-collection.html", "reports.html", "users.html", "administration.html",
    "events.html", "profile.html",
    "index.html", "ganti-password.html",
]


def _existing_views(html_path):
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    views = set(re.findall(r'data-view="([^"]+)"', html))
    ids = set(re.findall(r'id="([^"]+)"', html))
    return views | ids


def test_all_html_pages_exist():
    for page in PAGES:
        assert os.path.isfile(os.path.join(BASE_DIR, page)), f"Halaman hilang: {page}"


def test_nav_js_links_resolve():
    nav_path = os.path.join(BASE_DIR, "assets", "js", "nav.js")
    with open(nav_path, encoding="utf-8") as fh:
        nav = fh.read()
    hrefs = re.findall(r'href:\s*"([^"]+)"', nav)
    assert hrefs, "nav.js tidak memiliki tautan"
    for href in hrefs:
        page, _, anchor = href.partition("#")
        page_path = os.path.join(BASE_DIR, page)
        assert os.path.isfile(page_path), f"nav.js → file hilang: {href}"
        if anchor:
            views = _existing_views(page_path)
            assert anchor in views, f"nav.js → anchor hilang: {href}"


def test_html_hrefs_resolve():
    href_re = re.compile(r'href="([a-z0-9_-]+\.html)(?:#([a-z0-9_/-]+))?"')
    for page in PAGES:
        page_path = os.path.join(BASE_DIR, page)
        with open(page_path, encoding="utf-8") as fh:
            html = fh.read()
        for target, anchor in href_re.findall(html):
            target_path = os.path.join(BASE_DIR, target)
            assert os.path.isfile(target_path), f"{page} → file hilang: {target}"
            if anchor and not anchor.startswith("host/"):
                views = _existing_views(target_path)
                assert anchor in views, f"{page} → anchor hilang: {target}#{anchor}"


def test_inventory_detail_route_exists():
    page_path = os.path.join(BASE_DIR, "inventory.html")
    assert 'data-view="host"' in open(page_path, encoding="utf-8").read()
