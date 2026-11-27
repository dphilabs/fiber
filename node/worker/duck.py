# fiber.node.worker.duck
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from typing import Any

from fiber.node.bound.memory.sandbox import TabularSandbox
from xphi.arch.contract.protocol.binder import tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol

class DuckWorker(AsyncWorkerProtocol):
    def __init__(self, root: str = ".", max_rows: int = 200) -> None:
        self.root = Path(root).expanduser().resolve()
        self.sandbox = TabularSandbox(self.root)
        self.max_rows = max(1, max_rows)
        super().__init__(agent_name="worker.duck")

    @tool
    async def describe_data(self, source: str) -> dict[str, Any]:
        """Inspect column names and types of a local tabular file (CSV, TSV, JSONL, Parquet, XLSX).

        Args:
            source: File path relative to workspace root (e.g., 'logs/access.jsonl').
        """
        columns = await asyncio.to_thread(self.sandbox.describe, source)
        return {"source": source, "columns": columns}

    @tool
    async def query_sql(self, sql: str, limit: int = 50) -> dict[str, Any]:
        """Execute a read-only DuckDB SQL query against local data files.

        Args:
            sql: Read-only SQL statement (SELECT, WITH, DESCRIBE, SUMMARIZE, EXPLAIN).
            limit: Max rows to return (default 50, max 200).
        """
        capped_limit = max(1, min(limit, self.max_rows))
        rows = await asyncio.to_thread(self.sandbox.query, sql, capped_limit)
        return {"row_count": len(rows), "limit": capped_limit, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--max-rows", type=int, default=200)
    args = parser.parse_args()
    worker = DuckWorker(root=args.root, max_rows=args.max_rows)
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()