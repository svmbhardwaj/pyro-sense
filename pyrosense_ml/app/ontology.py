"""Canonical hotspot classification ontology — versioned, single source.

Phase A (plan §23): every part of the system (Node BFF, ML service, frontend)
must speak ONE label vocabulary. The ML feature schema may keep its own
training class names; those map HERE, never sideways between consumers.

Ontology v2 labels
------------------
agricultural_burning        crop/stubble/field burns, seasonal, rural
forest_vegetation           wildfire in forest/shrub/grass fuels
industrial_process          factory/plant fires, flaring, process heat
energy_infrastructure       power plants, substations, pipelines, refineries
mining_extraction           mines, quarries, smelters
persistent_thermal_source   long-lived steady thermal signature (flare,
                            kiln, furnace) without a discrete incident
unknown                     insufficient evidence / below confidence floor

Versioning contract (plan §24): every persisted prediction carries
ontology_version alongside model_version / feature_schema_version /
dataset_version. Bumping ONTOLOGY_VERSION means the label set or mapping
semantics changed — it is NOT tied to model retraining.
"""

from __future__ import annotations

from typing import Final, Literal

ONTOLOGY_VERSION: Final[str] = "2"

OntologyLabel = Literal[
    "agricultural_burning",
    "forest_vegetation",
    "industrial_process",
    "energy_infrastructure",
    "mining_extraction",
    "persistent_thermal_source",
    "unknown",
]

ONTOLOGY_LABELS: Final[tuple[OntologyLabel, ...]] = (
    "agricultural_burning",
    "forest_vegetation",
    "industrial_process",
    "energy_infrastructure",
    "mining_extraction",
    "persistent_thermal_source",
    "unknown",
)

# Legacy vocabularies that exist in the repo today (plan §23):
#
# 1. Node classificationService rule labels (strings like "Industrial Fire").
# 2. The MLP classifier's training classes in feature_schema.CLASSIFIER_CLASSES
#    (e.g. "Agricultural", "Industrial", "Mining").
# 3. The old GBM's classes (subset of #2 plus "Wildfire").
#
# Each maps to exactly one canonical label. Anything unmappable -> "unknown"
# (never silently dropped: abstention is part of the contract, plan §27).

_LEGACY_NODE_LABEL_MAP: Final[dict[str, OntologyLabel]] = {
    "industrial_fire": "industrial_process",
    "gas_flare": "persistent_thermal_source",
    "crop_burning": "agricultural_burning",
    "mining_activity": "mining_extraction",
    "wildfire": "forest_vegetation",
    "persistent_source": "persistent_thermal_source",
    "normal_operations": "unknown",
}

_LEGACY_ML_LABEL_MAP: Final[dict[str, OntologyLabel]] = {
    "agricultural": "agricultural_burning",
    "forest_vegetation": "forest_vegetation",
    "forest": "forest_vegetation",
    "industrial": "industrial_process",
    "infrastructure_energy": "energy_infrastructure",
    "energy_infrastructure": "energy_infrastructure",
    "mining": "mining_extraction",
    "wildfire": "forest_vegetation",
}

# Sub-label mapping: canonical label -> more specific Node-era label. Kept for
# display/back-compat only; the canonical label is what is stored and served.
DISPLAY_SUBTYPE_MAP: Final[dict[OntologyLabel, str]] = {
    "industrial_process": "Industrial Fire",
    "persistent_thermal_source": "Gas Flare / Persistent Source",
    "agricultural_burning": "Crop Burning",
    "mining_extraction": "Mining Activity",
    "forest_vegetation": "Wildfire",
    "energy_infrastructure": "Energy Infrastructure",
    "unknown": "Unknown / Needs Review",
}


def normalize_to_ontology(raw_label: str | None) -> OntologyLabel:
    """Map any legacy/vocabulary label to the canonical ontology.

    Matching is case-insensitive on snake_case/space/hyphen-normalized input.
    Unrecognized labels return "unknown" — callers should treat that as
    abstention (needs_review), not as a failure.
    """
    if not raw_label:
        return "unknown"
    key = raw_label.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _LEGACY_ML_LABEL_MAP:
        return _LEGACY_ML_LABEL_MAP[key]
    if key in _LEGACY_NODE_LABEL_MAP:
        return _LEGACY_NODE_LABEL_MAP[key]
    if key in ONTOLOGY_LABELS:  # already canonical
        return key  # type: ignore[return-value]
    return "unknown"


def is_canonical(label: str) -> bool:
    """True if label is already a canonical v2 label (no mapping needed)."""
    return label in ONTOLOGY_LABELS


def display_label(label: OntologyLabel) -> str:
    """Human-facing name for UI badges (canonical label -> display string)."""
    return DISPLAY_SUBTYPE_MAP.get(label, "Unknown / Needs Review")
