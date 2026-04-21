"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage:
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import ui
from config import (
    AGENTS_FILE,
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    KNOWLEDGE_DIR,
    SCRIPTS_DIR,
    now_iso,
)
from utils import (
    file_hash,
    list_raw_files,
    load_state,
    read_wiki_index,
    save_state,
)

# ── Paths for the LLM to use ──────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent


class _Tee:
    """File-like that mirrors writes to multiple streams."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        n = 0
        for s in self._streams:
            n = s.write(data)
            s.flush()
        return n

    def flush(self) -> None:
        for s in self._streams:
            s.flush()

    def isatty(self) -> bool:
        return any(getattr(s, "isatty", lambda: False)() for s in self._streams)


async def compile_daily_log(log_path: Path, state: dict) -> float:
    """Compile a single daily log into knowledge articles.

    Returns the API cost of the compilation.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    ingested_entry = state.get("ingested", {}).get(log_path.name, {})
    offset = ingested_entry.get("last_compiled_byte_offset", 0)
    with open(log_path, "rb") as f:
        f.seek(offset)
        delta_bytes = f.read()
    delta_content = delta_bytes.decode("utf-8", errors="replace")
    previous_last_compile_at = state.get("last_compile_at") or "never"

    if not delta_content.strip():
        print(f"  No new content since last compile, skipping {log_path.name}.")
        return 0.0

    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()

    timestamp = now_iso()

    prompt = f"""You are a knowledge compiler. Your job is to read new entries from a daily
conversation log and merge them into a set of structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{wiki_index}

## Existing Wiki Articles

Full article bodies are not inlined here to keep this prompt small. The index above lists every existing article with a one-line summary — use that to decide wikilink targets and whether a concept is already covered. When you decide to update an existing article, use the `Read` tool to fetch its current body before calling `Edit`.

## New Entries Since Last Compile ({previous_last_compile_at})

**File:** {log_path.name}

{delta_content}

## Your Task

Read the new entries above and merge them into the existing wiki articles (listed in the index above; read bodies on demand). Update existing articles rather than replacing them; create new articles only for genuinely new concepts. Follow the schema exactly.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article
2. **Create concept articles** in `knowledge/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Use the `Read` tool to fetch the existing article's current body, then add the new information and append the source to the frontmatter
5. **Update knowledge/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. **Append to knowledge/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""

    cost = 0.0

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=30,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass  # compilation output - LLM writes files directly
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"  Cost: ${cost:.4f}")
    except Exception as e:
        print(f"  Error: {e}")
        return 0.0

    # Update state - advance the offset so the next compile only sees new content
    rel_path = log_path.name
    state.setdefault("ingested", {})[rel_path] = {
        "hash": file_hash(log_path),
        "compiled_at": now_iso(),
        "cost_usd": cost,
        "last_compiled_byte_offset": log_path.stat().st_size,
    }
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(state)

    return cost


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    ui.enable_utf8_console()

    state = load_state()

    # Determine which files to compile
    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            # Try resolving relative to project root
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                rel = log_path.name
                prev = state.get("ingested", {}).get(rel, {})
                if not prev or prev.get("hash") != file_hash(log_path):
                    to_compile.append(log_path)

    # Force recompile: reset offsets so the full log is re-sent to the LLM.
    if args.all or args.file:
        for log_path in to_compile:
            entry = state.get("ingested", {}).get(log_path.name)
            if entry:
                entry["last_compiled_byte_offset"] = 0

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return

    exit_code = 0
    compile_log = SCRIPTS_DIR / "compile.log"
    log_handle = open(compile_log, "a", encoding="utf-8")
    original_stdout = sys.stdout
    sys.stdout = _Tee(original_stdout, log_handle)

    try:
        log_handle.write(f"\n--- compile started {now_iso()} ---\n")
        log_handle.flush()

        ui.print_banner(ROOT_DIR.parent.name)
        start_time = time.monotonic()
        stop_event, spin_thread = ui.start_spinner(start_time)

        try:
            total_cost = 0.0
            for i, log_path in enumerate(to_compile, 1):
                ui.clear_spinner_line()
                print(f"[{i}/{len(to_compile)}] Compiling {log_path.name}...")
                cost = asyncio.run(compile_daily_log(log_path, state))
                total_cost += cost
                ui.clear_spinner_line()
                print(f"  Done.")

            articles = list_wiki_articles()
            ui.clear_spinner_line()
            print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
            print(f"Knowledge base: {len(articles)} articles")

            # Arm the cooldown only on a clean full-run exit
            state["last_compile_at"] = now_iso()
            save_state(state)
        except Exception as e:
            ui.clear_spinner_line()
            print(f"Error: {e}", file=sys.stderr)
            exit_code = 1
        finally:
            stop_event.set()
            spin_thread.join(timeout=1.0)
            duration = time.monotonic() - start_time
            ui.print_footer(
                exit_code,
                duration,
                log_file=compile_log if exit_code != 0 else None,
            )
    finally:
        sys.stdout = original_stdout
        log_handle.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
