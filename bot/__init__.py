"""The WhatsApp bot: inbound message -> this week's stored digest.

Deliberately standalone — nothing here imports `isb_events`. The package
depends on `libsql-experimental`, a compiled extension that fights serverless
runtimes, so the bot reaches Turso over its HTTP API with plain `httpx`
instead (see CLAUDE.md § Delivery). The pipeline and the bot share a database
table, never a process.
"""
