"""E-commerce browser skill for product search and comparison.

Wraps the existing A11y / vision cascade with e-commerce-specific logic:
  - Skip Layer 1 (HTML extract) — Amazon.in and Flipkart require JavaScript
    to render product listings; a plain GET returns empty page chrome.
  - EcommerceA11yDriver: A11yDriver subclass with a system prompt that walks
    the standard e-commerce workflow (search → filter → sort → scroll → done).
  - Post-navigation product extraction: one V9 /v1/chat call turns the
    trafilatura-extracted page text into a typed products list.
  - Vision escalation: if A11y navigates but the page content is empty
    (canvas-heavy or fully JS-deferred), falls through to SetOfMarksDriver.

Metadata keys accepted by the Planner (set in NodeSpec.metadata):
  url               (required)  Base site URL.
                                "https://www.amazon.in"  or
                                "https://www.flipkart.com"
  goal              (required)  Free-text description of what to find, e.g.
                                "Find top 3 mechanical wireless keyboards
                                 under ₹8,000, sort by customer rating"
  product_query     (optional)  Explicit search terms. Parsed from `goal`
                                when absent.
  price_max         (optional)  Upper price bound in ₹ (integer or string).
  product_count     (optional)  How many products to return (default 3).
  required_features (optional)  List of feature strings to filter on, e.g.
                                ["mechanical", "wireless"] or
                                ["QHD", "Type-C", "27 inch"].

Output: AgentResult.output is a dict with:
  products        list[dict]  Structured product records (see PRODUCT_LIST_SCHEMA)
  product_count   int
  filters_applied list[str]
  sort_applied    str
  total_visible   str
  site            str         "amazon_in" | "flipkart" | "generic"
  product_query   str
  path            str         "ecommerce_a11y" | "ecommerce_vision"
  turns           int
  final_url       str
  actions         list[dict]
  extraction_note str

No Distiller node is required downstream — the skill already returns
structured per-product records. Wire directly to Formatter.
"""
from __future__ import annotations

import asyncio
import time

from schemas import AgentResult, NodeSpec

from .client import V9Client
from .driver import A11yDriver, SetOfMarksDriver
from .skill import BrowserSkill, detect_gateway_block  # noqa: F401 (detect_gateway_block used by _drive)


# ── e-commerce A11y system prompt ─────────────────────────────────────────────
#
# This replaces the generic A11y prompt for all turns inside an e-commerce
# session.  The key differences from SYSTEM_PROMPT_A11Y:
#   - Explicit 7-step workflow (search → wait → price filter → feature filters
#     → sort → scroll → done)
#   - Site-specific hints embedded in the goal string (not the system prompt)
#     so the same prompt works for both Amazon and Flipkart.
#   - Strict 1-action-per-turn rule (dropdown stacking is the most common
#     failure mode on these sites).
#   - Explicit "do NOT open product detail pages" guard — the driver stays
#     on the listing page; extraction happens after the driver exits.

ECOMMERCE_A11Y_PROMPT = """\
You are a browser-driving agent specialized in e-commerce product search.
Each turn you receive a text legend of visible interactive elements:
  [id]<tag role="role">name</tag>
and the current PAGE URL. No screenshot is available.

YOUR ONLY JOB: Navigate the site to reach a search-results page that shows
the products matching the search query with the requested filters applied.
You do NOT read, list, or compare products — that is handled separately after
you call done(). Simply reach the right filtered/sorted state.

═══ STANDARD WORKFLOW ═══

STEP 1 — SEARCH  (2 turns)
  Turn A: Locate the main search textbox/searchbox near the top of the page.
          Emit: type(<id>, "<product query>", clear=true)
          This is the ONLY action this turn.
  Turn B: key("Enter")
          This is the ONLY action this turn.
  Never bundle type + key in the same turn.

STEP 2 — WAIT  (1 turn)
  After the search submits: wait(seconds=2)
  This gives the JS results page time to render.

STEP 3 — PRICE FILTER  (0–3 turns; skip if no price limit given)
  Look in the sidebar or filter panel for one of:
    • A link/label whose text contains the target price range,
      e.g. "Under ₹1,000", "₹5,000 - ₹10,000", "Up to ₹8000"
    • Input boxes labeled "Min" / "Max" / "From" / "To" with a Go / → button

  PRICE BRACKET SELECTION RULE — critical:
    When choosing a price bracket link, pick the bracket whose UPPER BOUND
    is equal to or GREATER THAN your price_max.
    Example: price_max=₹8,000 → correct choices are "Under ₹8,000" or
    "₹5,000 – ₹10,000". NEVER pick "₹1,000 – ₹5,000" or "Under ₹5,000"
    because those exclude products priced ₹5,000–₹8,000.
    If in doubt, use the Min/Max input instead: leave Min blank, type price_max
    into Max/To, then click Go.

  After clicking a price filter: emit wait(seconds=1) as the next standalone turn.
  Apply ONE price filter only. Skip if no filter is visible in the legend.

STEP 4 — FEATURE FILTERS  (0–2 turns; skip if no features requested)
  Look for checkboxes or links in the sidebar that match requested features,
  e.g. "Wireless", "Mechanical", "Bluetooth", "Type-C", "27 inch", "QHD".
  Click the single best-matching checkbox/link, one per turn, max 2 turns.
  Skip silently if no matching filter appears in the legend.

STEP 5 — SORT  (1–2 turns)
  Find the sort control. Two possible layouts:
    DROPDOWN (Amazon-style): Label says "Sort by: Featured" or similar.
      Turn A: click(<id of the sort dropdown>) — the ONLY action this turn.
      Turn B: click(<id of preferred sort option>):
              Prefer in order:
                "Avg. Customer Review" > "Customer Rating" > "Popularity"
                > "Price: Low to High" > "Featured"
    TAB-STYLE (Flipkart-style): Sort options are inline links/buttons.
      Just click the preferred one directly in one turn.
  CRITICAL: Never put a dropdown-click and a selection-click in the same turn.
  The options only appear on the next turn's legend.

STEP 6 — SCROLL  (1–2 turns)
  scroll(direction="down", amount=900) one or two times to load product cards.

STEP 7 — DONE
  Emit: done(success=true, note="filters and sort applied")

═══ STRICT RULES ═══
  • ONE action per turn. No exceptions for dropdown steps.
  • done() must be the ONLY action in its turn. NEVER bundle done() with
    scroll, wait, or any other action. done() is always alone.
  • After clicking any price filter, feature filter, or sort option:
    emit wait(seconds=1) as the NEXT standalone turn before proceeding.
  • After any click that opens a dropdown or popup:
    wait one turn before acting again.
  • Do NOT navigate to individual product detail pages. Stay on the list.
  • If you see a CAPTCHA, login wall, or "sign in to continue":
    done(success=false, note="gateway_blocked") immediately.
  • If the search input is not visible, scroll up first.
  • After 18 turns without done, emit done(success=false, note="turn_cap_reached").
  • Be terse in `thinking` — one sentence maximum.
"""


# ── product extraction schema ─────────────────────────────────────────────────
#
# Strict JSON schema passed to V9 /v1/chat as response_format.
# Every field is a string so the LLM never has to choose between int/float/str
# for prices and counts that vary in formatting across sites.

PRODUCT_LIST_SCHEMA: dict = {
    "type": "object",
    "required": ["products"],
    "additionalProperties": False,
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["rank", "name", "price"],
                "additionalProperties": False,
                "properties": {
                    "rank":           {"type": "integer",
                                       "description": "1-based position in results"},
                    "name":           {"type": "string",
                                       "description": "Full product name as shown"},
                    "brand":          {"type": "string"},
                    "price":          {"type": "string",
                                       "description": "Current price, e.g. '₹5,999'"},
                    "original_price": {"type": "string",
                                       "description": "Pre-discount price if shown"},
                    "discount":       {"type": "string",
                                       "description": "Discount shown, e.g. '33% off'"},
                    "rating":         {"type": "string",
                                       "description": "e.g. '4.3 out of 5 stars'"},
                    "review_count":   {"type": "string",
                                       "description": "e.g. '12,345 ratings'"},
                    "key_specs":      {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 6,
                        "description": "Up to 6 spec bullets from title or highlights",
                    },
                    "availability":   {"type": "string",
                                       "description": "e.g. 'In Stock', 'Only 2 left'"},
                },
            },
        },
        "filters_applied": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Human-readable list of filters the driver applied",
        },
        "sort_applied":    {"type": "string"},
        "total_visible":   {"type": "string",
                            "description": "Result count shown, e.g. 'Over 1,000 results'"},
        "extraction_note": {"type": "string",
                            "description": "Any extraction caveat or warning"},
    },
}

# JavaScript run inside the Playwright page just before the browser closes.
# Returns {cards, totalVisible, sortApplied, filterPills} so one evaluate()
# call captures everything needed without re-opening the browser.
#
# Improvements over v1:
#   - Selector fallback chain for name and price (handles layout variants)
#   - xl-size price selector is the actual buy price, not a sub-price
#   - ASIN-based deduplication (Amazon shows the same item as both sponsored
#     and organic; keep first occurrence)
#   - Noise regex strips coupon/delivery/shipping lines from spec bullets
#   - Discount badge extracted directly from the red badge element
#   - Availability text captured (e.g. "Only 3 left in stock")
#   - Page-level totalVisible, sortApplied, filterPills extracted once
_AMAZON_JS_EXTRACTOR = """\
(() => {
  // ── page-level metadata ─────────────────────────────────────────────────
  // Result count — find a span inside the info bar that contains "results"
  // Avoid .a-color-state which holds the search query text, not the count.
  const infoBar = document.querySelector('[data-component-type="s-result-info-bar"]');
  const countSpan = infoBar
    ? Array.from(infoBar.querySelectorAll('span')).find(
        el => /\\bresults?\\b/i.test(el.textContent) && el.children.length <= 2
      )
    : null;
  const totalVisible = countSpan
    ? countSpan.textContent.replace(/\\s+/g, ' ').trim()
    : '';

  const sortEl = document.querySelector('#a-autoid-0-announce, .a-dropdown-prompt');
  const sortApplied = sortEl?.textContent?.trim() || '';

  const filterPills = Array.from(document.querySelectorAll(
    'span[data-component-type="s-breadcrumb"] span.a-color-base,' +
    ' #n-title span.a-color-state'
  )).map(el => el.textContent.trim()).filter(Boolean);

  // ── per-card extraction ─────────────────────────────────────────────────
  const NOISE = /coupon|delivery|ships|sold by|import fee|eligible|prime|\\bdays\\b|return/i;
  const seen  = new Set();
  const results = [];

  for (const el of document.querySelectorAll('[data-component-type="s-search-result"]')) {
    const asin = el.getAttribute('data-asin') || '';
    if (asin && seen.has(asin)) continue;
    if (asin) seen.add(asin);

    // name — class-specific span is most precise, fall through to generic
    const name =
      el.querySelector('h2 a span[class*="a-text-normal"]')?.textContent?.trim() ||
      el.querySelector('h2 span')?.textContent?.trim() ||
      el.querySelector('h2 a span')?.textContent?.trim() || '';
    if (!name) continue;

    // price — xl-size is the headline buy price
    const price =
      el.querySelector('.a-price[data-a-size="xl"] .a-offscreen')?.textContent?.trim() ||
      el.querySelector('.a-price .a-offscreen')?.textContent?.trim() || '';
    if (!price) continue;

    const orig         = el.querySelector('.a-text-price .a-offscreen')?.textContent?.trim() || '';
    const discountBadge= el.querySelector('.a-badge-text')?.textContent?.trim() || '';
    const rating       = el.querySelector('.a-icon-alt')?.textContent?.trim() ||
                         el.querySelector('[aria-label*="out of 5"]')?.getAttribute('aria-label') || '';
    const rvEl         = el.querySelector('[aria-label$="ratings"],[aria-label$="reviews"],[aria-label*="ratings"]');
    const reviews      = rvEl?.getAttribute('aria-label') || '';
    const brand        = el.querySelector('.a-color-secondary .a-text-bold')?.textContent?.trim() || '';
    const availability = el.querySelector('.a-color-price')?.textContent?.trim() || '';

    // Spec bullets — Amazon search cards may use li.a-list-item or .a-row spans
    const bulletEls = el.querySelectorAll(
      'ul.a-unordered-list li.a-list-item,' +
      ' .a-row.a-size-base .a-list-item,' +
      ' .a-row.a-size-mini span.a-color-base'
    );
    const bullets = Array.from(bulletEls)
      .map(e => e.textContent.trim())
      .filter(s => s.length > 5 && s.length < 100 && !NOISE.test(s))
      .slice(0, 5);

    results.push({asin, name, price, orig, discountBadge, rating, reviews, brand, availability, bullets});
    if (results.length >= 20) break;
  }

  return {cards: results, totalVisible, sortApplied, filterPills};
})()
"""


_EXTRACTION_SYSTEM = """\
You are a data-extraction assistant. Given raw text scraped from an e-commerce
search-results page, extract structured product records in the exact JSON schema
provided. Extract ONLY products that appear in the text — do not invent data.
Prices are shown in ₹ with comma separators.  Ratings appear as "X.X out of 5"
or "X.X ⭐".  Review counts appear as "N,NNN ratings" or "N,NNN reviews".
Key specs come from product titles, sub-titles, and bullet-point highlights.
Return the top products by relevance order as they appear in the text.
"""


# ── specialized driver ────────────────────────────────────────────────────────

class EcommerceA11yDriver(A11yDriver):
    """A11yDriver with the e-commerce 7-step navigation system prompt.

    Overrides step() to wait for DOM stability before each enumeration.
    E-commerce sites (especially Flipkart) navigate on search/filter clicks;
    calling page.evaluate() while a navigation is still in-flight raises
    "Execution context was destroyed".  Waiting for domcontentloaded before
    every turn's enumerate_interactives() call eliminates this class of error.

    Overrides _decide() to enforce single-action-per-turn at Python level.
    Even when the LLM bundles [type, click] or [scroll, done] in one response,
    this intercept keeps only the first action (or done() when present) so
    each turn has exactly one observable effect and the price-filter sequence
    executes correctly.
    """
    SYSTEM_PROMPT = ECOMMERCE_A11Y_PROMPT
    LAYER_NAME = "ecommerce_a11y"

    async def _decide(self, snap, turn: int):
        parsed, result = await super()._decide(snap, turn)
        if parsed:
            actions = list(parsed.get("actions") or [])
            if len(actions) > 1:
                done_acts = [a for a in actions if a.get("type") == "done"]
                if done_acts:
                    # done() always runs alone — drop any scroll/wait bundled with it
                    parsed["actions"] = done_acts[:1]
                else:
                    # Execute only the first action; re-decide next turn with fresh legend
                    parsed["actions"] = [actions[0]]
        return parsed, result

    async def _try_apply_price_filter_direct(self, price_max: int) -> bool:
        """Deterministically apply the Amazon price filter via Playwright.

        Bypasses the LLM entirely for this step.  Tries two strategies:
          1. Min/Max input fields — fill Max=price_max and click Go.
             This is the most precise approach; produces an exact ceiling.
          2. Exact-ceiling sidebar link — href encodes exactly p_36:-N where
             N/100 == price_max (e.g. "Under ₹8,000").
             Range links (e.g. "₹5,000–₹10,000") are intentionally skipped;
             they would include over-budget results.

        Returns True if the URL shows p_36 (filter confirmed applied).
        Amazon encodes prices in paise: ₹8,000 = p_36:-800000 in the URL.
        """
        import re as _re
        from urllib.parse import urlparse, parse_qs, unquote as _unquote

        def _price_applied() -> bool:
            params = parse_qs(urlparse(self.page.url).query)
            return "p_36" in _unquote(params.get("rh", [""])[0])

        if _price_applied():
            return True

        # Give the sidebar 3 s to mount (it loads after the main product grid)
        try:
            await self.page.wait_for_selector(
                'a[href*="p_36"], #high-price, input[name*="price_ceiling"]',
                timeout=3_000,
            )
        except Exception:  # noqa: BLE001
            pass  # proceed; selectors may have different names

        # ── Strategy 1: Min/Max input fields (most precise) ──────────────
        # Typing price_max into the Max field produces an exact ceiling
        # filter (p_36:-N), unlike range links which always over-include.
        try:
            max_input = await self.page.query_selector(
                "#high-price, "
                "input[name='field-price_ceiling'], "
                "input[name*='highPrice'], "
                "input[placeholder='Max']"
            )
            if max_input:
                await max_input.fill(str(price_max))
                go_btn = await self.page.query_selector(
                    "#go-button, "
                    "input[type='submit'][value='Go'], "
                    "input.a-button-text[type='submit']"
                )
                if go_btn:
                    await go_btn.click()
                else:
                    await max_input.press("Enter")
                try:
                    await self.page.wait_for_load_state(
                        "domcontentloaded", timeout=6_000
                    )
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1.0)
                if _price_applied():
                    return True
        except Exception:  # noqa: BLE001
            pass

        # ── Strategy 2: exact-ceiling sidebar link ────────────────────────
        # Only click a link whose ceiling == price_max exactly.
        # Range links (lower-upper form) are skipped to avoid over-inclusion.
        try:
            links = await self.page.query_selector_all('a[href*="p_36"]')
            for link in links:
                href = _unquote(await link.get_attribute("href") or "")
                # "up to" form: p_36:-800000  (negative value = ceiling only)
                m = _re.search(r'p_36:(-\d+)(?:[,&\s]|$)', href)
                if m:
                    ceiling = abs(int(m.group(1))) // 100
                    if ceiling == price_max:
                        await link.click()
                        try:
                            await self.page.wait_for_load_state(
                                "domcontentloaded", timeout=6_000
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        await asyncio.sleep(1.0)
                        if _price_applied():
                            return True
                        break  # tried and failed — don't loop further
        except Exception:  # noqa: BLE001
            pass

        return False

    async def step(self, turn: int) -> tuple[bool, bool, str]:
        # Wait for any pending navigation to settle before enumerating the DOM.
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:  # noqa: BLE001 — timeout or already stable, continue
            pass
        # Brief pause for JS frameworks to mount dynamic components.
        await asyncio.sleep(0.3)

        # Compute once; used by both the deterministic filter and the phase
        # goal replacement below.
        import re as _re
        from urllib.parse import urlparse, parse_qs, unquote as _unquote
        price_match = _re.search(
            r"PRICE FILTER\s*[≤<]\s*₹([\d,]+)", self.config.goal
        )

        # After a search submit (key="Enter"), Amazon's sidebar mounts AFTER
        # the product grid.  Wait 2 s then immediately try to apply the price
        # filter via direct Playwright interaction — no LLM involvement for
        # this step.  If that succeeds the URL will carry p_36 and the phase
        # goal replacement below is skipped automatically.
        if self.steps:
            prev = self.steps[-1].actions or []
            if any(
                a.get("type") == "key" and
                str(a.get("value", "")).lower() in ("enter", "return")
                for a in prev
            ):
                await asyncio.sleep(2.0)
                if price_match and "amazon.in" in self.page.url:
                    price_num = int(price_match.group(1).replace(",", ""))
                    if "/s?" in self.page.url or "?k=" in self.page.url:
                        await self._try_apply_price_filter_direct(price_num)

        # Phase goal replacement (fallback): fires only when the deterministic
        # filter above did not apply the filter (URL still has no p_36).
        original_goal = self.config.goal
        if price_match and turn >= 3:
            current_url = self.page.url
            url_params = parse_qs(urlparse(current_url).query)
            rh_val = _unquote(url_params.get("rh", [""])[0])
            on_amazon_results = (
                "amazon.in" in current_url
                and ("/s?" in current_url or "?k=" in current_url)
            )
            if on_amazon_results and "p_36" not in rh_val:
                price_str = price_match.group(1)
                self.config.goal = (
                    f"Apply the price filter on this Amazon search-results page."
                    f" In the LEFT SIDEBAR find a price range link that covers"
                    f" up to ₹{price_str} — for example 'Under ₹{price_str}'"
                    f" or '₹5,000–₹10,000' — and click it."
                    f" If the sidebar shows Min/Max input boxes instead,"
                    f" type {price_str} into the Max field (leave Min blank)."
                    f" Do NOT click the sort dropdown yet."
                )

        try:
            stop, success, note = await super().step(turn)
        finally:
            self.config.goal = original_goal  # always restore

        # After done() signals end-of-navigation, wait for scroll-triggered
        # lazy-loaded product cards to finish rendering before JS extraction.
        if stop:
            await asyncio.sleep(1.5)

        return stop, success, note


# ── main skill ────────────────────────────────────────────────────────────────

def _parse_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


class EcommerceBrowserSkill(BrowserSkill):
    """E-commerce product search and comparison skill.

    Inherits BrowserSkill._drive() (Playwright browser management, gateway-
    block detection, artifact saving) and overrides run() to:
      1. Skip Layer 1 (HTML extract) — e-commerce pages are JS-rendered.
      2. Use EcommerceA11yDriver with the specialized navigation prompt.
      3. Add a post-navigation LLM extraction step that turns page text into
         a typed products list.
      4. Escalate to SetOfMarksDriver if A11y finds no extractable content.
    """
    NAME = "ecommerce_browser"

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _detect_site(url: str) -> str:
        u = url.lower()
        if "amazon.in" in u:
            return "amazon_in"
        if "flipkart.com" in u:
            return "flipkart"
        return "generic"

    @staticmethod
    def _build_nav_goal(
        product_query: str,
        price_max: int | None,
        product_count: int,
        features: list[str],
        site: str,
    ) -> str:
        """Construct the goal string passed to the EcommerceA11yDriver.

        The system prompt describes the 7-step workflow; the goal string carries
        the specifics (what to search, what price cap, which features, how many
        products to surface). Numbered steps are used so the driver cannot
        reorder them (e.g. doing sort before price filter).
        Site-specific navigation hints are appended so the driver can adapt
        without branching logic in the system prompt.
        """
        feat_str = ", ".join(f'"{f}"' for f in features) if features else ""

        n = 1
        lines: list[str] = []
        lines.append(f"{n}. Search for '{product_query}'")
        n += 1
        if price_max:
            lines.append(
                f"{n}. Apply PRICE FILTER ≤ ₹{price_max:,}"
                f" — you MUST do this BEFORE sorting"
            )
            n += 1
        if feat_str:
            lines.append(
                f"{n}. Apply feature filters if visible: {feat_str}"
            )
            n += 1
        lines.append(f"{n}. Sort by customer rating (highest first)")
        n += 1
        lines.append(
            f"{n}. Scroll to ensure at least {product_count} product cards are visible"
        )
        n += 1
        lines.append(f"{n}. done(success=true, note='navigation complete')")

        goal = "EXECUTE STEPS IN THIS EXACT ORDER:\n" + "\n".join(lines)

        amazon_price_hint = ""
        if price_max:
            amazon_price_hint = (
                f" For the price filter, look for 'Under ₹{price_max:,}' first;"
                f" if absent, choose the range whose upper bound is ≥ ₹{price_max:,}"
                f" (e.g. for ₹{price_max:,} use '₹5,000–₹10,000', NOT 'Under ₹5,000')."
                f" If only Min/Max inputs are visible, type {price_max:,} into the Max"
                f" field, then click Go in the NEXT turn."
            )

        site_hints = {
            "amazon_in": (
                " [Amazon.in hints] Search box label is 'Search Amazon.in'."
                " Sort dropdown label is 'Sort by: Featured' — click it, then on the"
                " NEXT turn click 'Avg. Customer Review'."
                " Price filter links appear in the left sidebar."
                + amazon_price_hint
            ),
            "flipkart": (
                " [Flipkart hints] If a login/location popup or overlay appears"
                " (close button, ✕, or 'Skip'), click it to dismiss BEFORE searching."
                " Search box is in the top navigation bar."
                " Sort options are inline tab-style links (not a dropdown) —"
                " click 'Customer Rating' directly in one turn."
                " Price and feature filters appear in the left sidebar panel."
            ),
        }
        return goal + site_hints.get(site, "")

    async def _extract_products(
        self,
        client: V9Client,
        content: str,
        product_query: str,
        count: int,
    ) -> dict:
        """One LLM call that parses page text into structured product records.

        Uses the strict PRODUCT_LIST_SCHEMA so the gateway forces valid JSON.
        Falls back to an empty products list on any error so the overall skill
        result stays successful (navigation worked; extraction is best-effort).
        """
        if not content or len(content.strip()) < 80:
            return {
                "products": [],
                "extraction_note": "page content too sparse for product extraction",
            }

        prompt = (
            f"Search query: '{product_query}'\n"
            f"Extract the top {count} products from the page text below.\n"
            f"Fill every field you can confirm from the text; omit fields you cannot.\n\n"
            f"--- PAGE TEXT ---\n{content[:14_000]}\n--- END ---"
        )
        try:
            result = await client.chat(
                prompt,
                system=_EXTRACTION_SYSTEM,
                schema=PRODUCT_LIST_SCHEMA,
                schema_name="ProductList",
                max_tokens=2048,
            )
            return result.parsed or {
                "products": [],
                "extraction_note": "extraction response did not parse",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "products": [],
                "extraction_note": f"extraction LLM call failed: {exc}",
            }

    # ── JS extraction hook (Amazon) ───────────────────────────────────────────

    async def _post_page_hook(self, page, result) -> None:
        """Called by BrowserSkill._drive() before the browser closes.
        Runs the Amazon JS extractor and stores the full result dict so
        run() can use both the cards and the page-level metadata."""
        if getattr(self, "_site_for_hook", "") != "amazon_in":
            return
        try:
            js_result = await page.evaluate(_AMAZON_JS_EXTRACTOR) or {}
            # Only store if we actually got cards — empty dict triggers fallback
            result.js_result = js_result if js_result.get("cards") else {}
        except Exception:  # noqa: BLE001
            result.js_result = {}

    @staticmethod
    def _js_cards_to_schema(
        js_result: dict, count: int, price_max: int | None = None
    ) -> dict:
        """Convert the JS extractor result dict to PRODUCT_LIST_SCHEMA format.

        Uses page-level totalVisible / sortApplied / filterPills directly
        from the browser.  Computes discount percentage when the badge text
        is absent but orig_price > price.

        price_max: when set, cards whose parsed price exceeds this value are
        filtered out in Python before slicing to `count`.  This is a safety
        net that guarantees in-budget results even when the browser-level
        price filter could not be applied.
        """
        cards        = js_result.get("cards") or []
        total_visible = js_result.get("totalVisible") or ""
        sort_applied  = js_result.get("sortApplied") or ""
        filter_pills  = js_result.get("filterPills") or []

        # Python-level price gate: keep only cards whose price ≤ price_max.
        # Applied BEFORE slicing so the top `count` are always in-budget.
        if price_max is not None:
            in_budget = [
                c for c in cards
                if (_parse_int(c.get("price")) or 0) <= price_max
            ]
            if in_budget:  # don't discard everything if parsing fails for all
                cards = in_budget

        products = []
        for i, c in enumerate(cards[:count], 1):
            price_str = c.get("price", "")
            orig_str  = c.get("orig", "")

            # Use discount badge if present; otherwise compute from prices
            discount = c.get("discountBadge", "")
            if not discount and price_str and orig_str:
                p = _parse_int(price_str)
                o = _parse_int(orig_str)
                if p and o and o > p:
                    discount = f"{round((o - p) / o * 100)}% off"

            # Key specs: prefer explicit bullet elements; fall back to splitting
            # the product title on commas/pipes (Amazon packs specs into titles)
            key_specs = (c.get("bullets") or [])[:6]
            if not key_specs:
                name = c.get("name", "")
                import re as _re
                parts = _re.split(r"[,|]", name)
                key_specs = [
                    p.strip() for p in parts[1:]
                    if 4 < len(p.strip()) < 70
                ][:5]

            # Strip pipe characters — they break markdown table formatting
            raw_name = c.get("name", "").replace("|", "-").strip(" -")
            key_specs = [s.replace("|", "-") for s in key_specs]

            products.append({
                "rank":           i,
                "name":           raw_name,
                "brand":          c.get("brand", ""),
                "price":          price_str,
                "original_price": orig_str,
                "discount":       discount,
                "rating":         c.get("rating", ""),
                "review_count":   c.get("reviews", ""),
                "key_specs":      key_specs,
                "availability":   c.get("availability", ""),
            })

        return {
            "products":        products,
            "filters_applied": filter_pills,
            "sort_applied":    sort_applied,
            "total_visible":   total_visible,
            "extraction_note": f"DOM JS: {len(cards)} cards on page, {len(products)} returned",
        }

    @staticmethod
    def _parse_url_filters(final_url: str) -> dict:
        """Decode price filter and sort order from the Amazon search URL.

        Used to fill in filters_applied / sort_applied when the JS extractor's
        page-level metadata comes back blank (happens on some layout variants).
        Amazon encodes prices in paise: p_36:-800000 = Up to ₹8,000.
        """
        from urllib.parse import urlparse, parse_qs, unquote
        out: dict = {"filters": [], "sort": ""}
        try:
            params = parse_qs(urlparse(final_url).query)
            rh = unquote(params.get("rh", [""])[0])
            for seg in rh.split(","):
                seg = seg.strip()
                if seg.startswith("p_36:"):
                    tail = seg.split(":")[-1]
                    if tail.startswith("-"):
                        rupees = int(tail[1:]) // 100
                        out["filters"].append(f"Under ₹{rupees:,}")
            sort_map = {
                "review-rank":     "Avg. Customer Review",
                "price-asc-rank":  "Price: Low to High",
                "price-desc-rank": "Price: High to Low",
                "relevancerank":   "Featured",
            }
            out["sort"] = sort_map.get(params.get("s", [""])[0], "")
        except Exception:  # noqa: BLE001
            pass
        return out

    # ── public entry point ────────────────────────────────────────────────────

    async def run(self, node: NodeSpec) -> AgentResult:  # type: ignore[override]
        t0 = time.time()

        # ── resolve metadata ──────────────────────────────────────────────────
        url = (
            node.metadata.get("url")
            or node.metadata.get("site_url")
            or ""
        )
        if not url:
            return self._pack_error(
                "", "ecommerce search", "interaction_failed",
                "metadata.url is required for ecommerce_browser "
                "(e.g. 'https://www.amazon.in')",
            )

        goal_raw      = node.metadata.get("goal") or ""
        product_query = node.metadata.get("product_query") or goal_raw or "product search"
        price_max     = _parse_int(node.metadata.get("price_max"))
        product_count = int(node.metadata.get("product_count") or 3)
        features: list[str] = list(node.metadata.get("required_features") or [])
        site          = self._detect_site(url)
        nav_goal      = self._build_nav_goal(
            product_query, price_max, product_count, features, site
        )

        client = V9Client(
            base_url=self.gateway_url,
            agent=self.agent_tag,
            session=self.session,
        )
        artifacts_dir = (
            str(self.artifacts_root / f"ecommerce_{int(t0)}")
            if self.artifacts_root else None
        )

        # ── Phase 1: navigation — A11y first (cheaper), vision fallback ───────
        # E-commerce workflows need more turns than generic pages (search + wait
        # + price filter + feature filters + sort + scroll = 8–14 steps minimum
        # before done).  We floor at 20 regardless of the configured default.
        a11y_max_steps = max(self.max_steps_a11y, 20)

        # Tell _post_page_hook which site we're on so it can run site-specific
        # JS extraction before the browser closes.
        self._site_for_hook = site

        nav = await self._drive(
            EcommerceA11yDriver,
            url, nav_goal, client, artifacts_dir,
            self.a11y_provider_pin, a11y_max_steps,
        )

        if getattr(nav, "gateway_blocked", False):
            return self._pack_error(
                url, nav_goal, "gateway_blocked",
                nav.note or "gateway_blocked after JS render",
                elapsed=time.time() - t0,
            )

        extracted: str = getattr(nav, "extracted", "") or ""
        path_used = "ecommerce_a11y"

        # Escalate to vision only when A11y failed AND the page yielded no text
        # at all (indicates a canvas-only or severely JS-deferred page).
        # If A11y navigation "failed" but trafilatura still got product text,
        # we skip vision and go straight to extraction.
        if not nav.success and not extracted:
            nav = await self._drive(
                SetOfMarksDriver,
                url, nav_goal, client, artifacts_dir,
                self.vision_provider_pin, self.max_steps_vision,
            )
            if getattr(nav, "gateway_blocked", False):
                return self._pack_error(
                    url, nav_goal, "gateway_blocked",
                    nav.note or "gateway_blocked",
                    elapsed=time.time() - t0,
                )
            extracted = getattr(nav, "extracted", "") or ""
            path_used = "ecommerce_vision"

        # If both layers produced nothing at all, surface a clean error.
        if not extracted and not nav.success:
            return self._pack_error(
                url, nav_goal, "interaction_failed",
                f"navigation failed with no extractable content: {nav.note}",
                elapsed=time.time() - t0,
            )

        # ── Phase 2: structured product extraction ────────────────────────────
        # Amazon.in: use DOM JS cards from _post_page_hook (trafilatura strips
        # prices/titles).  After schema conversion, fill any blank filter/sort
        # fields from the final URL so the output is always complete.
        # All other sites: LLM extraction from trafilatura text.
        js_result: dict = getattr(nav, "js_result", None) or {}
        js_cards: list[dict] = js_result.get("cards") or []
        if js_cards:
            products_data = self._js_cards_to_schema(
                js_result, product_count, price_max=price_max
            )
            # URL-decode is a reliable fallback when page-level JS metadata
            # is blank (layout variants, A/B tests on Amazon's front-end)
            final_url = getattr(nav, "final_url", "") or ""
            url_meta = self._parse_url_filters(final_url)
            if url_meta["filters"] and not products_data.get("filters_applied"):
                products_data["filters_applied"] = url_meta["filters"]
            if url_meta["sort"] and not products_data.get("sort_applied"):
                products_data["sort_applied"] = url_meta["sort"]
        else:
            products_data = await self._extract_products(
                client, extracted, product_query, product_count
            )

        return self._pack_ecommerce(
            url, nav_goal, nav, products_data,
            site=site,
            product_query=product_query,
            path_used=path_used,
            elapsed=time.time() - t0,
        )

    # ── packer ────────────────────────────────────────────────────────────────

    def _pack_ecommerce(
        self,
        url: str,
        goal: str,
        nav_result,
        products_data: dict,
        *,
        site: str,
        product_query: str,
        path_used: str,
        elapsed: float,
    ) -> AgentResult:
        products: list[dict] = products_data.get("products") or []
        output = {
            # identity
            "url":            url,
            "goal":           goal,
            "site":           site,
            "product_query":  product_query,
            "path":           path_used,
            # navigation metadata
            "turns":          int(getattr(nav_result, "turns", 0) or 0),
            "final_url":      str(getattr(nav_result, "final_url", url) or url),
            "actions":        list(getattr(nav_result, "actions", []) or []),
            # extraction results — these are what the Formatter renders
            "products":       products,
            "product_count":  len(products),
            "filters_applied": list(products_data.get("filters_applied") or []),
            "sort_applied":   str(products_data.get("sort_applied") or ""),
            "total_visible":  str(products_data.get("total_visible") or ""),
            "extraction_note": str(products_data.get("extraction_note") or ""),
        }
        return AgentResult(
            success=True,
            agent_name=self.NAME,
            output=output,
            elapsed_s=elapsed,
        )
