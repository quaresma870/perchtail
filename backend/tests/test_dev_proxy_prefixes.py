"""`npm run dev` proxies a fixed list of top-level paths to the backend
(`apiPrefixes` in frontend/vite.config.ts). A router added without its
prefix there works in production and fails only in development, so the list
is compared here with the top-level path of every route the app has."""

import re
from pathlib import Path

from app.main import app
from starlette.routing import Mount

VITE_CONFIG = Path(__file__).resolve().parents[2] / "frontend" / "vite.config.ts"
# FastAPI's own pages: not part of the API the frontend calls.
FASTAPI_PAGES = {"docs", "redoc", "openapi.json"}


def _route_paths(routes) -> list[str]:
    """Every route's full path. FastAPI keeps an included router as one
    entry whose routes carry the router's prefix through its contexts."""
    paths = []
    for route in routes:
        if isinstance(route, Mount):
            continue  # the built frontend, served at "/"
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            for context in contexts():
                if isinstance(context.original_route, Mount):
                    continue
                paths.append(context.path_format)
        else:
            paths.append(route.path)
    return paths


def _backend_prefixes() -> set[str]:
    prefixes = set()
    for path in _route_paths(app.routes):
        first = path.strip("/").split("/", 1)[0]
        if first and first not in FASTAPI_PAGES:
            prefixes.add(f"/{first}")
    return prefixes


def _vite_prefixes() -> list[str]:
    text = VITE_CONFIG.read_text(encoding="utf-8")
    block = re.search(r"const apiPrefixes = \[(.*?)\]", text, re.DOTALL)
    assert block is not None, "apiPrefixes not found in frontend/vite.config.ts"
    return re.findall(r"'([^']+)'", block.group(1))


def test_the_dev_proxy_names_every_top_level_api_path():
    vite = _vite_prefixes()
    assert len(vite) == len(set(vite)), "a prefix is listed twice"
    assert set(vite) == _backend_prefixes()
