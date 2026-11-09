"""Artifact writer: the working copy of every artifact lives in the UC Volume.

Layout (design doc section 10): /Volumes/<catalog>/artifacts/files/<source_id>/<run_id>/<stage>/attempt_<n>/<name>
Create-only: files are never overwritten. A redraft or rerun gets a new attempt folder.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Any

from databricks.sdk import WorkspaceClient

STAGE_DIRS = {
    0: "s0_smoke", 1: "s1_profile", 2: "s2_enrich", 3: "s3_glossary", 4: "s4_model",
    5: "s5_metric_views", 6: "s6_genie_assignment", 7: "s7_genie_build", 8: "s8_benchmarks",
}
_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*$")


@dataclass
class ArtifactWriter:
    catalog: str
    source_id: str
    run_id: str
    stage: int
    attempt: int
    client: WorkspaceClient | None = None

    def __post_init__(self) -> None:
        self.client = self.client or WorkspaceClient()

    @property
    def base_path(self) -> str:
        return (f"/Volumes/{self.catalog}/artifacts/files/{self.source_id}/{self.run_id}/"
                f"{STAGE_DIRS[self.stage]}/attempt_{self.attempt}")

    def path_for(self, name: str) -> str:
        if not _SAFE.match(name) or ".." in name:
            raise ValueError(f"unsafe artifact name: {name!r}")
        return f"{self.base_path}/{name}"

    def write_text(self, name: str, content: str) -> str:
        path = self.path_for(name)
        self.client.files.upload(path, io.BytesIO(content.encode("utf-8")), overwrite=False)
        return path

    def write_json(self, name: str, obj: Any) -> str:
        return self.write_text(name, json.dumps(obj, indent=2, default=str, ensure_ascii=False))

    def read_text(self, name: str) -> str:
        return self.client.files.download(self.path_for(name)).contents.read().decode("utf-8")

    def list(self) -> list[str]:
        try:
            return [e.path for e in self.client.files.list_directory_contents(self.base_path)]
        except Exception:
            return []
