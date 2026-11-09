from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(script: str, storage_dir: Path) -> str:
    environment = dict(os.environ)
    environment["TC_STUDIO_STORAGE_DIR"] = str(storage_dir)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(PROJECT_ROOT),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_runtime_cache_modules_honor_shared_storage_directory(tmp_path):
    output = _run_isolated(
        """
import core.batch_presets as batch_presets
import core.common_specs_syncer as specs_syncer
import core.doc_syncer as doc_syncer
import core.tree_syncer as tree_syncer
import scripts.sync_live_catalog as live_sync

print(doc_syncer.STORAGE_DIR)
print(tree_syncer.STORAGE_DIR)
print(tree_syncer.CATALOG_PATH)
print(specs_syncer.SPECS_CACHE_PATH)
print(batch_presets.DOCS_DIR)
print(live_sync.OUTPUT_PATH)
print(live_sync.CACHE_DIR)
""",
        tmp_path,
    )
    paths = output.splitlines()[-7:]

    assert paths == [
        str(tmp_path / "docs"),
        str(tmp_path),
        str(tmp_path / "catalogs" / "tim_full_catalog.json"),
        str(tmp_path / "live_common_specs.json"),
        str(tmp_path / "docs"),
        str(tmp_path / "catalogs" / "live_full_catalog.json"),
        str(tmp_path / "docs_cache" / "live"),
    ]


def test_package_document_cache_is_read_only_fallback(tmp_path):
    output = _run_isolated(
        """
from core.doc_syncer import get_doc_by_id

document = get_doc_by_id(20458)
assert document is not None
assert document["doc_id"] == 20458
print("fallback-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("fallback-ok")
    assert not (tmp_path / "docs" / "20458.json").exists()


def test_common_specs_refreshes_when_another_process_publishes_runtime_cache(tmp_path):
    output = _run_isolated(
        """
import json
from pathlib import Path

from core.common_specs_syncer import CommonSpecsManager

manager = CommonSpecsManager()
static_specs = manager.get_specs("live")
assert static_specs["service"] == "live"

runtime_path = Path(__import__("os").environ["TC_STUDIO_STORAGE_DIR"]) / "live_common_specs.json"
runtime_specs = dict(static_specs)
runtime_specs["probe_marker"] = "runtime"
runtime_path.write_text(json.dumps(runtime_specs), encoding="utf-8")

refreshed = manager.get_specs("live")
assert refreshed["probe_marker"] == "runtime"
print("refresh-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("refresh-ok")


def test_invalid_runtime_specs_fall_back_to_package_snapshot(tmp_path):
    output = _run_isolated(
        """
from pathlib import Path

from core.common_specs_syncer import CommonSpecsManager

runtime_path = Path(__import__("os").environ["TC_STUDIO_STORAGE_DIR"]) / "live_common_specs.json"
runtime_path.write_text("{broken", encoding="utf-8")

specs = CommonSpecsManager().get_specs("live")
assert specs["service"] == "live"
assert "invocation" in specs
print("specs-fallback-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("specs-fallback-ok")


def test_failed_common_specs_sync_keeps_last_known_good_snapshot(tmp_path):
    output = _run_isolated(
        """
from pathlib import Path

import core.common_specs_syncer as module

module.fetch_doc_detail_api = lambda _doc_id: None
manager = module.CommonSpecsManager()
specs = manager.sync_all("live")

assert specs["service"] == "live"
assert "invocation" in specs
runtime_path = Path(__import__("os").environ["TC_STUDIO_STORAGE_DIR"]) / "live_common_specs.json"
assert not runtime_path.exists()
print("specs-preserved-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("specs-preserved-ok")


def test_invalid_runtime_catalog_falls_back_to_package_catalog(tmp_path):
    output = _run_isolated(
        """
from pathlib import Path

from core.registry import registry

runtime_path = Path(__import__("os").environ["TC_STUDIO_STORAGE_DIR"]) / "catalogs" / "tim_full_catalog.json"
runtime_path.parent.mkdir(parents=True, exist_ok=True)
runtime_path.write_text("{broken", encoding="utf-8")

assert registry.list_apis(service_type="im")
print("catalog-fallback-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("catalog-fallback-ok")


def test_empty_runtime_catalog_falls_back_to_package_catalog(tmp_path):
    output = _run_isolated(
        """
import json
from pathlib import Path

from core.registry import registry

runtime_path = Path(__import__("os").environ["TC_STUDIO_STORAGE_DIR"]) / "catalogs" / "tim_full_catalog.json"
runtime_path.parent.mkdir(parents=True, exist_ok=True)
runtime_path.write_text(json.dumps([]), encoding="utf-8")

assert registry.list_apis(service_type="im")
print("empty-catalog-fallback-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("empty-catalog-fallback-ok")


def test_live_doc_audit_falls_back_from_corrupt_runtime_catalog(tmp_path):
    output = _run_isolated(
        """
from pathlib import Path

from core.runtime_paths import runtime_catalog_path
from scripts.audit_and_sync_live_docs import _load_live_catalog

runtime_path = runtime_catalog_path("live")
runtime_path.parent.mkdir(parents=True, exist_ok=True)
runtime_path.write_text("{broken", encoding="utf-8")

catalog = _load_live_catalog()
assert catalog
assert all(isinstance(item, dict) for item in catalog)
print("audit-catalog-fallback-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("audit-catalog-fallback-ok")


def test_corrupt_runtime_catalog_does_not_block_tree_sync(tmp_path):
    output = _run_isolated(
        """
import json
from pathlib import Path

import core.tree_syncer as tree_syncer

runtime_path = Path(tree_syncer.CATALOG_PATH)
runtime_path.parent.mkdir(parents=True, exist_ok=True)
runtime_path.write_text("{broken", encoding="utf-8")
static_path = Path(tree_syncer.STATIC_CATALOG_PATH)
existing = json.loads(static_path.read_text(encoding="utf-8"))[0]

tree_syncer.fetch_official_hydration_tree = lambda: (
    {},
    [{
        "doc_id": existing["doc_id"],
        "title": existing["name"],
        "path": existing.get("path", []),
        "doc_url": existing.get("doc_url", ""),
    }],
)

result = tree_syncer.sync_catalog_from_live_tree()
assert result["status"] == "success"
published = json.loads(runtime_path.read_text(encoding="utf-8"))
assert published[0]["doc_id"] == existing["doc_id"]
print("corrupt-catalog-recovered")
""",
        tmp_path,
    )

    assert output.strip().endswith("corrupt-catalog-recovered")


def test_empty_tree_snapshot_does_not_replace_last_known_good_file(tmp_path):
    output = _run_isolated(
        """
import json
from pathlib import Path

import core.tree_syncer as tree_syncer

snapshot = Path(tree_syncer.STORAGE_DIR) / "official_live_tree.json"
snapshot.parent.mkdir(parents=True, exist_ok=True)
snapshot.write_text(json.dumps({"marker": "known-good"}), encoding="utf-8")

tree_syncer._save_tree_snapshot_if_usable({"marker": "empty"}, [])

assert json.loads(snapshot.read_text(encoding="utf-8"))["marker"] == "known-good"
print("empty-snapshot-preserved")
""",
        tmp_path,
    )

    assert output.strip().endswith("empty-snapshot-preserved")


def test_empty_tree_sync_refuses_to_replace_catalog(tmp_path):
    output = _run_isolated(
        """
from pathlib import Path

import core.tree_syncer as tree_syncer

tree_syncer.fetch_official_hydration_tree = lambda: ({}, [])
try:
    tree_syncer.sync_catalog_from_live_tree()
except RuntimeError as exc:
    assert "拒绝覆盖" in str(exc)
else:
    raise AssertionError("empty catalog sync unexpectedly succeeded")

assert not Path(tree_syncer.CATALOG_PATH).exists()
print("empty-catalog-rejected")
""",
        tmp_path,
    )

    assert output.strip().endswith("empty-catalog-rejected")


def test_registry_prefers_runtime_catalog_and_reloads_after_publish(tmp_path):
    output = _run_isolated(
        """
import json
from pathlib import Path

from core.file_storage import atomic_write_json
from core.registry import registry

catalog_path = Path(__import__("os").environ["TC_STUDIO_STORAGE_DIR"]) / "catalogs" / "probe_full_catalog.json"
first = {
    "api_id": "probe_one",
    "name": "Probe One",
    "service_type": "probe",
    "service_name": "probe",
    "command": "One",
    "action": "One",
    "fields": [],
    "sample_payload": {},
}
atomic_write_json(catalog_path, [first], mode=0o600)
assert registry.get_api("probe_one") is not None

second = dict(first)
second["api_id"] = "probe_two"
second["name"] = "Probe Two"
atomic_write_json(catalog_path, [second], mode=0o600)

assert registry.get_api("probe_two") is not None
assert registry.get_api("probe_one").api_id == "probe_two"
print("registry-refresh-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("registry-refresh-ok")


def test_tree_sync_publishes_runtime_catalog_without_modifying_package_catalog(tmp_path):
    output = _run_isolated(
        """
import json
from pathlib import Path

import core.tree_syncer as tree_syncer

static_path = Path(tree_syncer.STATIC_CATALOG_PATH)
static_before = static_path.read_bytes()
existing = json.loads(static_before)[0]

tree_syncer.fetch_official_hydration_tree = lambda: (
    {},
    [{
        "doc_id": existing["doc_id"],
        "title": existing["name"],
        "path": existing.get("path", []),
        "doc_url": existing.get("doc_url", ""),
    }],
)

result = tree_syncer.sync_catalog_from_live_tree()
runtime_path = Path(tree_syncer.CATALOG_PATH)
assert result["status"] == "success"
assert runtime_path.exists()
assert json.loads(runtime_path.read_text(encoding="utf-8"))[0]["doc_id"] == existing["doc_id"]
assert static_path.read_bytes() == static_before
print("tree-publish-ok")
""",
        tmp_path,
    )

    assert output.strip().endswith("tree-publish-ok")
