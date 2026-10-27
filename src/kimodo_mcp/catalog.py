"""Explicit generation model catalog for the reviewed Kimodo release."""

from typing import Literal

UPSTREAM_REVISION = "e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954"
ModelName = Literal[
    "kimodo-soma-rp-v1", "kimodo-soma-rp-v1.1", "kimodo-soma-rp-v1.2",
    "kimodo-soma-rp-ardyrep", "kimodo-smplx-rp", "kimodo-g1-rp",
    "kimodo-soma-seed-v1", "kimodo-soma-seed-v1.1", "kimodo-g1-seed",
]
OutputFormat = Literal["npz", "bvh", "amass", "csv"]

_MODELS = [
    ("kimodo-soma-rp-v1", "Kimodo-SOMA-RP-v1", "SOMA", "RP"),
    ("kimodo-soma-rp-v1.1", "Kimodo-SOMA-RP-v1.1", "SOMA", "RP"),
    ("kimodo-soma-rp-v1.2", "Kimodo-SOMA-RP-v1.2", "SOMA", "RP"),
    ("kimodo-soma-rp-ardyrep", "Kimodo-SOMA-RP-ArdyRep-v1.2", "SOMA", "RP"),
    ("kimodo-smplx-rp", "Kimodo-SMPLX-RP-v1", "SMPLX", "RP"),
    ("kimodo-g1-rp", "Kimodo-G1-RP-v1", "G1", "RP"),
    ("kimodo-soma-seed-v1", "Kimodo-SOMA-SEED-v1", "SOMA", "SEED"),
    ("kimodo-soma-seed-v1.1", "Kimodo-SOMA-SEED-v1.1", "SOMA", "SEED"),
    ("kimodo-g1-seed", "Kimodo-G1-SEED-v1", "G1", "SEED"),
]


def list_models() -> list[dict]:
    """Return metadata without importing Kimodo or downloading weights."""
    return [
        {
            "id": key, "name": name, "repo_id": f"nvidia/{name}",
            "skeleton": skeleton, "dataset": dataset,
            "output_joints": {"SOMA": 77, "SMPLX": 22, "G1": 34}[skeleton],
            "constraint_joints": {"SOMA": [30, 77], "SMPLX": [22], "G1": [34]}[skeleton],
            "formats": ["npz", {"SOMA": "bvh", "SMPLX": "amass", "G1": "csv"}[skeleton]],
            "postprocess_supported": skeleton != "G1",
            "reviewed_revision": UPSTREAM_REVISION,
        }
        for key, name, skeleton, dataset in _MODELS
    ]


def model_info(name: str) -> dict:
    for info in list_models():
        if info["id"] == name:
            return info
    raise ValueError("Model must be an exact identifier from list_models.")
