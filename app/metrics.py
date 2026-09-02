"""Prometheus metrics per spec §Observability."""
from prometheus_client import Counter, Gauge, Histogram

LAST_SCAN = Gauge("agentmemory_last_scan_timestamp", "Unix time of last completed vault sweep")
LAST_IMPORT = Gauge("agentmemory_last_import_timestamp", "Unix time of last export import")
EMBED_BACKLOG = Gauge("agentmemory_embed_backlog_chunks", "Chunks awaiting embedding")
CHUNKS = Gauge("agentmemory_chunks_total", "Chunks indexed")
DOCS = Gauge("agentmemory_documents_total", "Documents indexed")
STAGING = Gauge("agentmemory_staging_items", "Unfiled items by state",
                ["state"])  # unproposed | pending_approval | below_floor
PROPOSALS = Gauge("agentmemory_filing_proposals", "Proposals by status", ["status"])
AUTO_APPLIED = Counter("agentmemory_filing_auto_applied_total", "Auto-mode filings applied")
BELOW_FLOOR = Counter("agentmemory_filing_below_floor_total", "Proposals under confidence floor")
DEP_UP = Gauge("agentmemory_dependency_up", "Dependency reachability", ["dep"])
SEARCHES = Counter("agentmemory_search_requests_total", "Search requests")
SEARCH_LAT = Histogram("agentmemory_search_latency_seconds", "Search latency")
IMPORT_RUNS = Counter("agentmemory_import_runs_total", "Import runs", ["result"])
CHECK_FAILS = Counter("agentmemory_check_failures_total",
                      "unpack --check failures (completeness contract)")
