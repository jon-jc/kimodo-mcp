# Kimodo MCP

A local MCP server for generating character animations with [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo).
Supports standard MCP clients through stdio or loopback Streamable HTTP. The bridge is lightweight;
Kimodo and its GPU dependencies run in a separate Python environment.

Features include validated text timelines and kinematic constraints, durable asynchronous jobs,
cross-client GPU serialization, cancellation, reproducible request manifests, and native motion exports.

Install with `pip install -e ".[dev]"`, then run `kimodo-mcp`.
Set `KIMODO_REPO` to a trusted upstream checkout, `KIMODO_PYTHON` to its Python executable,
and `KIMODO_DATA_DIR` to a writable directory for jobs. Model downloads need Hugging Face access
to the upstream text encoder. This bridge does not generate meshes or automatically retarget arbitrary rigs.

Kimodo source and model weights retain their upstream licenses and are not bundled here.
