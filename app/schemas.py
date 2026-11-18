"""Pydantic request/response schemas."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Treatment = Literal["A", "B"]


# ---------- requests ----------

class PlotImport(BaseModel):
    """One plot in a bulk import (area + fresh weight; moisture optional)."""

    block_id: int = Field(ge=1, le=12)
    treatment: Treatment
    area: float = Field(gt=0, description="plot area, m^2")
    fresh_weight: float = Field(ge=0, description="fresh weight, kg")
    moisture: Optional[float] = Field(
        default=None, ge=0, lt=1,
        description="moisture fraction, e.g. 0.62; omit if not assayed yet",
    )


class ImportRequest(BaseModel):
    plots: list[PlotImport] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "ImportRequest":
        keys = [(p.block_id, p.treatment) for p in self.plots]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate (block_id, treatment) in request")
        return self


class MoistureEntry(BaseModel):
    block_id: int = Field(ge=1, le=12)
    treatment: Treatment
    moisture: float = Field(ge=0, lt=1, description="moisture fraction")


class MoistureRequest(BaseModel):
    entries: list[MoistureEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "MoistureRequest":
        keys = [(e.block_id, e.treatment) for e in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate (block_id, treatment) in request")
        return self


class Correction(BaseModel):
    """Correct one existing plot; only the provided fields are changed.

    Passing ``"moisture": null`` explicitly clears a wrong moisture value
    (the plot becomes pending again until re-assayed).
    """

    block_id: int = Field(ge=1, le=12)
    treatment: Treatment
    area: Optional[float] = Field(default=None, gt=0)
    fresh_weight: Optional[float] = Field(default=None, ge=0)
    moisture: Optional[float] = Field(default=None, ge=0, lt=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "Correction":
        provided = self.model_fields_set - {"block_id", "treatment"}
        if not provided:
            raise ValueError("provide at least one of area, fresh_weight, moisture")
        return self


class CorrectionRequest(BaseModel):
    corrections: list[Correction] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "CorrectionRequest":
        keys = [(c.block_id, c.treatment) for c in self.corrections]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate (block_id, treatment) in request")
        return self


# ---------- responses ----------

class MutationResponse(BaseModel):
    data_version: int
    changed: int
    unchanged: int


class PlotState(BaseModel):
    block_id: int
    treatment: str
    area: float
    fresh_weight: float
    moisture: Optional[float]
    updated_at: str


class BlockComplete(BaseModel):
    block_id: int
    dry_a: float
    dry_b: float
    diff: float = Field(description="dry matter B - A per unit area")


class BlockPending(BaseModel):
    block_id: int
    missing: list[str]


class BlockStatus(BaseModel):
    data_version: int
    complete: list[BlockComplete]
    pending: list[BlockPending]


class ProvenancePlot(BaseModel):
    block_id: int
    treatment: str
    area: float
    fresh_weight: float
    moisture: Optional[float]
    updated_at: str


class Provenance(BaseModel):
    data_version: int
    source_hash: str = Field(description="sha256 over the canonical source rows")
    plots: list[ProvenancePlot]


class PublicationSummary(BaseModel):
    id: int
    created_at: str
    data_version: int
    stale: bool = Field(description="true once trial data changed after publication")
    n_blocks: int
    mean_diff: float
    p_value: float
    n_permutations: int


class Publication(PublicationSummary):
    blocks: list[BlockComplete]
    pending: list[BlockPending]
    provenance: Provenance
