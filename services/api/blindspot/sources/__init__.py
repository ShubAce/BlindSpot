"""Upstream connectors: one file per source (firms.py, celestrak.py, openaq.py, ...).

Pure fetch + parse through blindspot.http.fetch, returning plain Python data. No database writes:
jobs own those.
"""
