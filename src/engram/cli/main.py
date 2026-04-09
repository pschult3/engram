"""engram CLI.

Subcommands:
  engram mcp serve            — start the stdio MCP server (used by .mcp.json)
  engram hook session-start   — Claude Code SessionStart hook
  engram hook user-prompt-submit
  engram hook post-tool-use
  engram hook post-compact
  engram hook session-end
  engram bootstrap            — print the current bootstrap capsule
  engram search QUERY         — query the store from the terminal
  engram drain                — drain the JSONL event queue into the store
  engram reindex              — (re)embed units missing a vector embedding
  engram doctor               — show config + storage paths + status
"""

from __future__ import annotations

import json

import click

from .. import __version__
from ..config import load_config
from ..digest import build_bootstrap_capsule
from ..hooks import (
    handle_pre_compact,
    handle_post_compact,
    handle_post_tool_use,
    handle_session_end,
    handle_session_start,
    handle_user_prompt_submit,
)
from ..ingest import drain_queue
from ..retrieval import rank_for_prompt, search_memory
from ..storage import EventQueue, open_store


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """engram — local memory for Claude Code."""


# ---------------------------- mcp ----------------------------

@cli.group()
def mcp() -> None:
    """MCP server commands."""


@mcp.command("serve")
def mcp_serve() -> None:
    """Run the stdio MCP server."""
    from ..mcp.server import run_stdio

    run_stdio()


# ---------------------------- hooks ----------------------------

@cli.group()
def hook() -> None:
    """Claude Code hook entry points (read JSON on stdin)."""


@hook.command("session-start")
def _h_session_start() -> None:
    out = handle_session_start()
    if out:
        click.echo(out)


@hook.command("user-prompt-submit")
def _h_user_prompt_submit() -> None:
    out = handle_user_prompt_submit()
    if out:
        click.echo(out)


@hook.command("post-tool-use")
def _h_post_tool_use() -> None:
    click.echo(json.dumps(handle_post_tool_use()))


@hook.command("pre-compact")
def _h_pre_compact() -> None:
    out = handle_pre_compact()
    if out:
        click.echo(out)


@hook.command("post-compact")
def _h_post_compact() -> None:
    click.echo(json.dumps(handle_post_compact()))


@hook.command("session-end")
def _h_session_end() -> None:
    click.echo(json.dumps(handle_session_end()))


# ---------------------------- ops ----------------------------

@cli.command()
@click.option("--max-tokens", default=None, type=int, help="Override token budget for this run.")
def bootstrap(max_tokens: int | None) -> None:
    """Print the current bootstrap capsule for the active project."""
    cfg = load_config()
    store = open_store(cfg.db_path, cfg.project)
    try:
        click.echo(build_bootstrap_capsule(store, max_tokens=max_tokens))
    finally:
        store.close()


@cli.group()
def config() -> None:
    """Read or write per-project settings stored in the memory database."""


@config.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Print the value of a project setting (empty string if unset)."""
    cfg = load_config()
    store = open_store(cfg.db_path, cfg.project)
    try:
        click.echo(store.get_setting(key))
    finally:
        store.close()


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Persist a project setting in the memory database.

    \b
    Recognised keys
    ---------------
    bootstrap_max_tokens   Target token cap for the SessionStart capsule (default 400).
    """
    cfg = load_config()
    store = open_store(cfg.db_path, cfg.project)
    try:
        store.set_setting(key, value)
        click.echo(f"{key} = {value}")
    finally:
        store.close()


@cli.command()
@click.argument("query")
@click.option("--top-k", default=8, show_default=True)
def search(query: str, top_k: int) -> None:
    """Search durable memory from the terminal."""
    cfg = load_config()
    store = open_store(cfg.db_path, cfg.project)
    try:
        hits = search_memory(store, query, top_k=top_k)
        if not hits:
            click.echo("(no matches)")
            return
        click.echo(rank_for_prompt(hits, max_chars=4000))
    finally:
        store.close()


@cli.command()
def drain() -> None:
    """Drain the JSONL event queue into the SQLite store."""
    cfg = load_config()
    store = open_store(cfg.db_path, cfg.project)
    queue = EventQueue(cfg.queue_path)
    try:
        stats = drain_queue(store, queue)
        click.echo(json.dumps(stats, indent=2))
    finally:
        store.close()


@cli.command()
@click.option("--batch", default=64, show_default=True, help="Units per embedding batch.")
def reindex(batch: int) -> None:
    """(Re)embed all memory units missing a vector embedding.

    Requires engram[vector] to be installed (fastembed + sqlite-vec).
    With ENGRAM_EMBEDDER=stub the stub backend is used instead.
    """
    from ..retrieval.embeddings import get_embedder

    cfg = load_config()
    store = open_store(cfg.db_path, cfg.project)
    try:
        if not store.vec_enabled:
            click.echo(
                "sqlite-vec extension not available — install engram[vector] and retry.",
                err=True,
            )
            raise SystemExit(1)

        embedder = get_embedder()
        if embedder is None:
            click.echo(
                "No embedder available — install engram[vector] or set ENGRAM_EMBEDDER=stub.",
                err=True,
            )
            raise SystemExit(1)

        total = 0
        while True:
            pending = store.units_needing_embedding(limit=batch)
            if not pending:
                break
            texts = [f"{u.title}\n\n{u.body}" for u, _ in pending]
            vecs = embedder.embed_many(texts)
            for (unit, rowid), vec in zip(pending, vecs):
                blob = embedder.serialize(vec)
                with store.tx() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                        (rowid, blob),
                    )
                    c.execute(
                        "UPDATE memory_units SET embedded_at=? WHERE id=?",
                        (store._now_iso(), unit.id),
                    )
            total += len(pending)
            click.echo(f"  embedded {total} units…", err=True)

        click.echo(json.dumps({"reindexed": total}))
    finally:
        store.close()


@cli.command()
def doctor() -> None:
    """Show resolved configuration and storage paths."""
    from ..retrieval.embeddings import get_embedder

    cfg = load_config()
    queue = EventQueue(cfg.queue_path)
    store = open_store(cfg.db_path, cfg.project)
    try:
        unit_counts = store.count_units() if cfg.db_path.exists() else {}
        relation_counts = store.count_relations() if cfg.db_path.exists() else {}

        embedder = get_embedder()
        info = {
            "version": __version__,
            "home": str(cfg.home),
            "project": cfg.project,
            "project_dir": str(cfg.project_dir),
            "db_path": str(cfg.db_path),
            "db_exists": cfg.db_path.exists(),
            "queue_path": str(cfg.queue_path),
            "queue_pending": queue.pending_count(),
            "log_level": cfg.log_level,
            # vector
            "vec_enabled": store.vec_enabled,
            "embedder_available": embedder is not None,
            "embedder_model": embedder.model_name if embedder else None,
            "units_total": unit_counts.get("total", 0),
            "units_embedded": unit_counts.get("embedded", 0),
            "units_pending_embed": unit_counts.get("pending_embed", 0),
            # graph
            "relations_total": sum(relation_counts.values()),
            "relations_by_type": relation_counts,
        }
        click.echo(json.dumps(info, indent=2))
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    cli()
