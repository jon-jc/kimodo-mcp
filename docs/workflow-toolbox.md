# One workflow for Studio and MCP

Open **Workflow & tools** in the Studio navigation. This workspace reads the running server's capability catalog, so its operation list is the same one advertised to MCP clients. The current release has 103 tools, 20 ordered recipes and 15 domain schemas. Optional runtimes can be absent while discovery, planning and saved-result inspection remain usable.

## Start with the visual workspace

The shortcut buttons open Create, Review & fix, Export, FreeMoCap capture, single-video fitting, saved motion, Assets & props, or UniRig. These workspaces provide the most direct controls for common tasks. The tool library is useful for less common parameters, inspecting exact contracts, reproducing an agent call or continuing a workflow from another client.

Choose a recipe before combining operations. Each step states the required inputs, expected evidence, conditional execution and recovery behavior. **Open controls** selects that step's tool and copies its literal arguments into a draft. References such as `steps.prepare.handoff_id` describe values to obtain from a real prior response; the interface never invents these IDs or executes the recipe automatically.

The **Native controls** inventory describes six reviewed source families: Kimodo, FreeMoCap, EasyMocap, UniRig, Animate-inspired review work, and Blender/Unreal handoffs. It distinguishes integrated controls from features that remain in the original application, research code or a separate operator workflow. See [the complete coverage guide](native-controls.md). This release does not claim all-native parity with every upstream application.

## Use exact controls

1. Search by operation name or description, or choose a tool group. Selecting a tool only reads its contract.
2. The editor displays its description and read/action annotation. An action may write files, start work, cancel a job or change an editor's state; its normal preconditions still apply.
3. Edit required parameters. Optional parameters are omitted until **Set this value** is checked, preserving the server's own defaults. Explicit `false`, zero and `null` remain distinct from omission.
4. Scalars use individual controls. Nested requests, arrays, unions and transforms use JSON fields. **Full input contract** includes all nested definitions, enums, bounds and defaults. **Complete arguments JSON** allows editing the complete request without a simplified preset discarding advanced values.
5. Click **Run selected tool** once. The same Pydantic validation, local-origin restrictions, request token and tool implementation used elsewhere in Studio apply. The interface does not accept arbitrary Python, shell commands or unregistered tool names.
6. Inspect the response's actual status and evidence. Save the request/response receipt for reproducibility when useful. A job ID or accepted submission is not a finished animation. Follow the recipe's status and output verification steps.

The library never automatically retries an action. Closing its dialog does not cancel a submitted operation. Use the appropriate shared history, status and cancellation tools to recover the exact job. Do not resubmit an editor operation merely because its response was lost.

## Agent discovery

MCP clients already receive complete input schemas in `tools/list`. The additional `get_tool_schema` operation makes one exact registered contract available through the same discovery workflow as Studio:

```json
{"tool": "get_tool_schema", "arguments": {"tool_name": "prepare_prop_scene_handoff"}}
```

It returns the live tool name, description, input schema, output schema and annotations without invoking that tool. `get_capabilities.native_control_coverage` supplies the reviewed upstream matrix, including `full_native_parity=false` and explicit unsupported controls. `get_input_schema` remains useful for a reusable domain model such as `unirig_request` or `capture_retarget_request`.

## Connect the stages deliberately

| Stage | Useful workflow | Evidence needed before continuing |
| --- | --- | --- |
| Record a performance | `freemocap` | Completed processing/export receipt, calibrated geometry where required, original timing and verified BVH |
| Estimate from an ordinary video | `video-mocap` | Completed SMPL24 capture, source-video identity and confidence/missing-frame review; separate retarget setup still required |
| Generate from text | `saved-plans`, `generate` | Exact saved request, completed candidate and source identity |
| Correct motion | `correct-motion`, `capture-character` | Correct representation, original frame references, immutable changed result and reviewed measurements |
| Rig a new asset | `unirig` | Verified skeleton/skin output, bone and deformation review; a compatible animation retarget map is a separate requirement |
| Attach a weapon or asset | `asset-props` | Static prop, saved placement recipe and a verified fitted-character animation; resulting scene preserves character poses |
| Deliver to the game project | `import-unreal`, `capture-character` | Exact project and compatible skeleton/mesh, verified animation FBX and final editor report |

The fitted character, native generation rig, captured landmarks, estimated SMPL body and arbitrary UniRig skeleton are distinct data representations. Their IDs and joint names are not interchangeable. Where a conversion is not implemented, the workflow reports that boundary rather than pretending that naming a bone completes retargeting. Natural grips, two-handed contact, collision handling, face/finger animation and final artistic acceptance need additional authored work.
