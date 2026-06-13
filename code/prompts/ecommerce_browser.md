The E-commerce Browser skill searches for products on Amazon.in or Flipkart,
applies price and feature filters, sorts by customer rating, and returns a
structured list of matching products ready for comparison. It drives the live
website through a Playwright browser (no static HTML fetch — these sites require
JavaScript), executes at least three visible actions (search, filter, sort), and
produces a typed `products` list. No downstream Distiller is needed.

Required metadata:
  url              Base site URL, e.g. "https://www.amazon.in" or
                   "https://www.flipkart.com". Pass the homepage or category
                   root — do NOT add query parameters; the skill drives the
                   search box itself.
  goal             Free-text description of what to find, e.g.
                   "Find top 3 mechanical wireless keyboards under ₹8,000,
                    sort by customer rating."

Optional metadata:
  product_query    Explicit search terms (extracted from `goal` when absent).
  price_max        Maximum price in ₹ as an integer, e.g. 8000.
  product_count    Number of products to return (default 3).
  required_features  List of feature strings to filter on, e.g.
                   ["mechanical", "wireless"] or ["QHD", "Type-C", "27 inch"].

Output (AgentResult.output):
  products         list of product dicts, each with: rank, name, brand, price,
                   original_price, discount, rating, review_count, key_specs,
                   availability.
  filters_applied  list of filter descriptions actually applied.
  sort_applied     sort order used.
  total_visible    result count string from the page.
  path             "ecommerce_a11y" or "ecommerce_vision".
  turns            number of browser interaction turns used.
