"""Tests for the EcommerceBrowserSkill added in Session 9.

Two layers:

  Unit tests (no gateway, no network)
  ────────────────────────────────────
  All deterministic logic: site detection, int parsing, goal construction,
  schema structure, agent_config registration, skills.py dispatch branch,
  and __init__ exports.  These run via `pytest tests/` with the rest of the
  suite.

  Live integration tests (gateway + real sites, run manually)
  ────────────────────────────────────────────────────────────
  Driven by the __main__ block at the bottom.  Run:

    uv run python tests/test_ecommerce_skill.py              # both sites
    uv run python tests/test_ecommerce_skill.py amazon       # Amazon.in only
    uv run python tests/test_ecommerce_skill.py flipkart     # Flipkart only
    uv run python tests/test_ecommerce_skill.py flow         # full flow.py E2E

  Each live run saves a JSON trace to out/ecommerce/ for post-run inspection.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Make the parent code/ directory importable regardless of cwd.
HERE = Path(__file__).resolve().parent
CODE = HERE.parent
sys.path.insert(0, str(CODE))


# ── unit tests ────────────────────────────────────────────────────────────────

def test_imports():
    """All new public symbols resolve without error."""
    from browser.ecommerce_skill import (  # noqa: F401
        ECOMMERCE_A11Y_PROMPT,
        PRODUCT_LIST_SCHEMA,
        EcommerceA11yDriver,
        EcommerceBrowserSkill,
        _parse_int,
    )
    from browser import EcommerceA11yDriver, EcommerceBrowserSkill  # noqa: F401


def test_detect_site():
    from browser.ecommerce_skill import EcommerceBrowserSkill as S
    assert S._detect_site("https://www.amazon.in") == "amazon_in"
    assert S._detect_site("https://amazon.in/s?k=foo") == "amazon_in"
    assert S._detect_site("https://www.flipkart.com") == "flipkart"
    assert S._detect_site("https://flipkart.com/search?q=bar") == "flipkart"
    assert S._detect_site("https://www.myntra.com") == "generic"
    assert S._detect_site("https://example.com") == "generic"


def test_parse_int():
    from browser.ecommerce_skill import _parse_int
    assert _parse_int(8000) == 8000
    assert _parse_int("8000") == 8000
    assert _parse_int("8,000") == 8000
    assert _parse_int("₹8,000") == 8000
    assert _parse_int("₹20000") == 20000
    assert _parse_int(None) is None
    assert _parse_int("") is None
    assert _parse_int("bad") is None
    assert _parse_int("N/A") is None


def test_build_nav_goal_amazon():
    from browser.ecommerce_skill import EcommerceBrowserSkill as S
    goal = S._build_nav_goal(
        "mechanical wireless keyboard", 8000, 3,
        ["mechanical", "wireless"], "amazon_in",
    )
    assert "mechanical wireless keyboard" in goal
    assert "8,000" in goal
    assert '"mechanical"' in goal
    assert '"wireless"' in goal
    assert "customer rating" in goal.lower()
    assert "3 product" in goal
    assert "Amazon.in hints" in goal
    assert "Avg. Customer Review" in goal


def test_build_nav_goal_flipkart():
    from browser.ecommerce_skill import EcommerceBrowserSkill as S
    goal = S._build_nav_goal(
        "27 inch QHD monitor", 20000, 3,
        ["QHD", "Type-C", "27 inch"], "flipkart",
    )
    assert "27 inch QHD monitor" in goal
    assert "20,000" in goal
    assert '"QHD"' in goal
    assert '"Type-C"' in goal
    assert "Flipkart hints" in goal
    assert "Customer Rating" in goal


def test_build_nav_goal_no_price_no_features():
    from browser.ecommerce_skill import EcommerceBrowserSkill as S
    goal = S._build_nav_goal("laptop", None, 5, [], "generic")
    assert "laptop" in goal
    assert "₹" not in goal            # no price block
    assert "feature filter" not in goal  # no feature block
    assert "5 product" in goal


def test_product_list_schema_structure():
    from browser.ecommerce_skill import PRODUCT_LIST_SCHEMA as S
    assert S["type"] == "object"
    assert "products" in S["required"]
    items = S["properties"]["products"]["items"]
    assert items["type"] == "object"
    req = set(items["required"])
    assert req == {"rank", "name", "price"}
    props = items["properties"]
    for field in ("rank", "name", "brand", "price", "original_price",
                  "discount", "rating", "review_count", "key_specs", "availability"):
        assert field in props, f"missing field in PRODUCT_LIST_SCHEMA: {field!r}"
    assert props["key_specs"]["type"] == "array"
    assert props["key_specs"]["maxItems"] == 6
    assert props["rank"]["type"] == "integer"
    # All other fields should be strings (prices/counts contain ₹ and commas)
    for field in ("name", "brand", "price", "original_price", "discount",
                  "rating", "review_count", "availability"):
        assert props[field]["type"] == "string", \
            f"field {field!r} should be string, got {props[field]['type']!r}"


def test_ecommerce_a11y_driver_subclass():
    from browser.ecommerce_skill import EcommerceA11yDriver, ECOMMERCE_A11Y_PROMPT
    from browser.driver import A11yDriver
    assert issubclass(EcommerceA11yDriver, A11yDriver)
    assert EcommerceA11yDriver.SYSTEM_PROMPT is ECOMMERCE_A11Y_PROMPT
    assert EcommerceA11yDriver.LAYER_NAME == "ecommerce_a11y"
    # Must differ from the generic A11y prompt
    from browser.driver import SYSTEM_PROMPT_A11Y
    assert EcommerceA11yDriver.SYSTEM_PROMPT != SYSTEM_PROMPT_A11Y


def test_ecommerce_skill_subclass():
    from browser.ecommerce_skill import EcommerceBrowserSkill
    from browser.skill import BrowserSkill
    assert issubclass(EcommerceBrowserSkill, BrowserSkill)
    assert EcommerceBrowserSkill.NAME == "ecommerce_browser"


def test_agent_config_has_ecommerce_browser():
    import yaml
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text())
    assert "ecommerce_browser" in cfg, \
        "ecommerce_browser skill is missing from agent_config.yaml"
    entry = cfg["ecommerce_browser"]
    assert "prompt" in entry
    assert "description" in entry
    prompt_path = CODE / entry["prompt"]
    assert prompt_path.exists(), \
        f"prompt file referenced in yaml does not exist: {prompt_path}"


def test_skills_py_dispatch_branch():
    src = (CODE / "skills.py").read_text()
    assert 'skill.name == "ecommerce_browser"' in src, \
        "skills.py is missing the ecommerce_browser dispatch branch"
    assert "EcommerceBrowserSkill" in src, \
        "skills.py does not import EcommerceBrowserSkill"


def test_init_exports():
    import browser
    assert hasattr(browser, "EcommerceA11yDriver")
    assert hasattr(browser, "EcommerceBrowserSkill")
    assert hasattr(browser, "ECOMMERCE_A11Y_PROMPT")
    assert hasattr(browser, "PRODUCT_LIST_SCHEMA")


def test_no_url_returns_error():
    """run() with missing url must return error_code=interaction_failed."""
    from browser.ecommerce_skill import EcommerceBrowserSkill
    from schemas import NodeSpec
    sk = EcommerceBrowserSkill(session="test_no_url")
    node = NodeSpec(skill="ecommerce_browser", inputs=[],
                    metadata={"goal": "find keyboard"})
    result = asyncio.run(sk.run(node))
    assert result.success is False
    assert result.error_code == "interaction_failed"
    assert "metadata.url" in (result.error or "")


# ── live integration runner ───────────────────────────────────────────────────

LIVE_CASES = {
    "amazon": {
        "label":            "amazon_keyboard",
        "url":              "https://www.amazon.in",
        "goal":             "Find top 3 mechanical wireless keyboards under ₹8,000, sort by customer rating.",
        "product_query":    "mechanical wireless keyboard",
        "price_max":        8000,
        "product_count":    3,
        "required_features": ["mechanical", "wireless"],
        "_timeout":         150,
    },
    "flipkart": {
        "label":            "flipkart_monitor",
        "url":              "https://www.flipkart.com",
        "goal":             "Find top 3 27-inch QHD monitors with Type-C charging under ₹20,000, sort by customer rating.",
        "product_query":    "27 inch QHD monitor Type-C",
        "price_max":        20000,
        "product_count":    3,
        "required_features": ["QHD", "Type-C", "27 inch"],
        "_timeout":         200,
    },
}

OUT_DIR = CODE / "out" / "ecommerce"


async def run_live_case(case_key: str) -> dict:
    from browser.ecommerce_skill import EcommerceBrowserSkill
    from schemas import NodeSpec

    c = LIVE_CASES[case_key]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sess = f"ecom_{case_key}_{int(time.time())}"
    sk = EcommerceBrowserSkill(
        artifacts_root=str(OUT_DIR / case_key),
        session=sess,
    )
    node = NodeSpec(
        skill="ecommerce_browser",
        inputs=[],
        metadata={k: v for k, v in c.items() if k not in ("label", "_timeout")},
    )
    timeout = c.get("_timeout", 150)
    t0 = time.time()
    try:
        result = await asyncio.wait_for(sk.run(node), timeout=float(timeout))
    except asyncio.TimeoutError:
        return {"label": c["label"], "success": False,
                "error": f"{timeout}s timeout", "elapsed_s": float(timeout),
                "products": [], "turns": None, "path": "(timeout)"}
    except Exception as exc:  # noqa: BLE001
        return {"label": c["label"], "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.time() - t0, 1),
                "products": [], "turns": None, "path": "(exception)"}

    out = result.output
    products = out.get("products") or []
    return {
        "label":           c["label"],
        "success":         result.success,
        "error_code":      result.error_code,
        "error":           (result.error or "")[:300],
        "path":            out.get("path"),
        "turns":           out.get("turns"),
        "filters_applied": out.get("filters_applied"),
        "sort_applied":    out.get("sort_applied"),
        "total_visible":   out.get("total_visible"),
        "product_count":   out.get("product_count"),
        "extraction_note": out.get("extraction_note"),
        "elapsed_s":       round(time.time() - t0, 1),
        "final_url":       out.get("final_url"),
        "session":         sess,
        "products":        products,
    }


def _print_result(r: dict) -> None:
    ok = "✓" if r["success"] else "✗"
    print(f"\n  {ok}  {r['label']}")
    print(f"     path          : {r.get('path')}")
    print(f"     turns         : {r.get('turns')}")
    print(f"     elapsed       : {r.get('elapsed_s')}s")
    print(f"     products found: {r.get('product_count', 0)}")
    print(f"     filters       : {r.get('filters_applied')}")
    print(f"     sort          : {r.get('sort_applied')}")
    print(f"     total visible : {r.get('total_visible')}")
    if r.get("extraction_note"):
        print(f"     note          : {r['extraction_note']}")
    if r.get("error"):
        print(f"     error         : {r['error'][:200]}")
    products = r.get("products") or []
    if products:
        print(f"\n     --- Products ---")
        for p in products:
            specs = ", ".join(p.get("key_specs") or [])
            print(f"     {p.get('rank','?')}. {p.get('name','?')[:60]}")
            print(f"        Price  : {p.get('price','?')}  "
                  f"(was {p.get('original_price','—')}, {p.get('discount','—')})")
            print(f"        Rating : {p.get('rating','?')}  |  {p.get('review_count','?')}")
            print(f"        Specs  : {specs or '—'}")
    else:
        print(f"     (no products extracted)")


async def run_flow_e2e(query: str) -> None:
    """Full orchestrator run via flow.py for the ecommerce query."""
    import subprocess
    print(f"\n  Running flow.py: {query!r}")
    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "python", "flow.py", query,
        cwd=str(CODE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    print(stdout.decode())


async def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "both"

    print("\n══════════════════════════════════════════════════════════════")
    print("  E-Commerce Browser Skill — Live Integration Tests")
    print("══════════════════════════════════════════════════════════════")

    if mode == "flow":
        await run_flow_e2e(
            "Find and compare 3 best mechanical wireless keyboards under "
            "₹8,000 on Amazon.in — mention price, rating, and key specs."
        )
        return 0

    cases = (
        ["amazon", "flipkart"] if mode == "both"
        else [mode] if mode in LIVE_CASES
        else list(LIVE_CASES)
    )

    results = []
    for key in cases:
        print(f"\n  → running: {key}  ({LIVE_CASES[key]['url']})")
        r = await run_live_case(key)
        _print_result(r)
        results.append(r)

        trace_path = OUT_DIR / f"{key}_trace.json"
        trace_path.write_text(json.dumps(r, indent=2, default=str),
                              encoding="utf-8")
        print(f"\n     trace → {trace_path}")

    # Summary
    print("\n══════════════════════════════════════════════════════════════")
    print("  Summary")
    print("══════════════════════════════════════════════════════════════")
    all_ok = True
    for r in results:
        ok = "✓" if r["success"] else "✗"
        p_count = r.get("product_count", 0)
        print(f"  {ok}  {r['label']:<28}  "
              f"products={p_count}  turns={r.get('turns')}  "
              f"path={r.get('path')}  {r.get('elapsed_s')}s")
        if not r["success"]:
            all_ok = False

    print()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
