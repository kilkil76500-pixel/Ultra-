"""Export the complete football cache and reliability history.

The collector cache and ``predictions.db`` are intentionally kept separate
while the bot runs.  This module creates a portable, human-readable export
without changing or deleting either source.
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from engine import cache_store, tracking

EXPORT_SCHEMA_VERSION = 1


def _json_dump(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _prediction_rows() -> tuple[list[str], list[dict[str, Any]]]:
    db_path = Path(config.WEB_CACHE_DIR) / "predictions.db"
    if not db_path.exists():
        return [], []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM predictions ORDER BY id").fetchall()
    columns = [str(column) for column in (rows[0].keys() if rows else [])]
    return columns, [dict(row) for row in rows]


def _write_predictions_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_cache(output_dir: str | Path | None = None) -> dict[str, Any]:
    """Export current snapshots and reliability data into *output_dir*.

    By default the export is written below ``WEB_CACHE_DIR/exports`` so it is
    itself retained by the bot's cache and included in the release archive.
    The returned manifest is JSON-serialisable and contains no credentials.
    """
    root = Path(output_dir) if output_dir else Path(config.WEB_CACHE_DIR) / "exports"
    root.mkdir(parents=True, exist_ok=True)

    snapshots = cache_store.load_snapshots()
    columns, predictions = _prediction_rows()
    report = tracking.calibration_report()
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    _json_dump(root / "snapshots.json", snapshots)
    _json_dump(root / "reliability_predictions.json", predictions)
    _write_predictions_csv(root / "reliability_predictions.csv", columns, predictions)
    _json_dump(
        root / "reliability_summary.json",
        {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at": exported_at,
            "prediction_count": len(predictions),
            "settled_count": report.n_settled,
            "accuracy_1x2": report.accuracy_1x2,
            "brier_1x2": report.brier_1x2,
            "accuracy_btts": report.accuracy_btts,
            "accuracy_over25": report.accuracy_over25,
            "by_confidence": {
                label: {
                    "n": bucket.n,
                    "accuracy_1x2": bucket.accuracy_1x2,
                    "brier_avg": bucket.brier_avg,
                }
                for label, bucket in report.by_confidence.items()
            },
        },
    )

    db_path = Path(config.WEB_CACHE_DIR) / "predictions.db"
    if db_path.exists():
        shutil.copy2(db_path, root / "predictions.db")

    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": exported_at,
        "scan_method": "headless-complete-single-fetch-v19.11",
        "snapshot_count": len(snapshots),
        "prediction_count": len(predictions),
        "settled_prediction_count": report.n_settled,
        "files": [
            "snapshots.json",
            "reliability_predictions.json",
            "reliability_predictions.csv",
            "reliability_summary.json",
            "predictions.db",
        ],
    }
    _json_dump(root / "export_manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export Football Intelligence cache data")
    parser.add_argument("--output", help="destination directory (default: cache/exports)")
    args = parser.parse_args()
    print(json.dumps(export_cache(args.output), ensure_ascii=False, indent=2))