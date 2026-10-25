# RealiSIM item evidence

Private Actor used by RealiSIM to ground physical item specs in public web evidence.

**Input:** `items` (text only: label, description, optional visible text, material guess, size hint) and `maxResultsPerItem` (1–5, default 3).

**How:** for each item (3 at a time) it calls `apify/rag-web-browser` with a product-spec search query (raw HTTP, 1 GB), then
deterministically extracts JSON-LD `Product` data (name, brand, GTIN/SKU, weight, width/height/depth, material) and
dimension, weight and material mentions from page text. Values are normalised to cm and g.

**Output:** one dataset item per input item: `sources[]` (url, domain, title, truncated page text, JSON-LD products,
extracted mentions) and `candidate_specs[]`. Every value carries its `url` and an exact `quote` from the fetched text, so
a downstream model (Claude in RealiSIM) can cite it and RealiSIM can re-verify the citation. Nothing is accepted
automatically; users review values before they enter a simulation.
