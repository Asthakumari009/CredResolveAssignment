"""Architecture rules, checked by parsing imports (no execution)."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "smartdialer"


def _imports(folder: str) -> dict[str, set[str]]:
    out = {}
    for path in (PKG / folder).rglob("*.py"):
        mods = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        out[str(path.relative_to(PKG))] = mods
    return out


def test_pacing_cannot_reach_allocator_providers_db_or_safety():
    forbidden = ("smartdialer.allocator", "smartdialer.providers", "smartdialer.safety", "smartdialer.db",
                 "smartdialer.transitions", "psycopg", "httpx")
    for file, mods in _imports("pacing").items():
        bad = [m for m in mods if m.startswith(forbidden)]
        assert not bad, f"{file} imports {bad}: the pacing engine may only propose"


def test_domain_is_pure():
    for file, mods in _imports("domain").items():
        bad = [m for m in mods if m.startswith("smartdialer.") and not m.startswith(("smartdialer.domain", "smartdialer.config"))
               or m.split(".")[0] in ("psycopg", "httpx", "fastapi", "os", "socket", "requests")]
        assert not bad, f"{file} imports {bad}: domain code must not do I/O"
