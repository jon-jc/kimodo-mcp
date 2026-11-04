# Model library and animated props

Use **Assets & props** to import weapons, swords and other game assets, place them in the scene or attach them to the registered character's bones, and review the resulting animation. The same library, attachment recipes, builds and verified downloads are available through MCP. Importing a model or saving a placement does not execute Blender or change a character animation.

## Import and inspect a model

Studio accepts a file selection or a local source path, a label and a library kind: `prop` or `character`. MCP uses `import_studio_asset(source_path, label, kind)`. GLB, FBX, OBJ and VRM are accepted, with a maximum of 512 MiB per file. Prefer a self-contained GLB or FBX with embedded textures. OBJ material sidecars and external textures are not copied into managed storage. The OBJ preview is geometry-only; the VRM preview does not preserve VRM-specific behaviors.

The library copies original bytes into `<KIMODO_DATA_DIR>/assets/<asset_id>`. The asset ID is its full SHA-256 digest. Reimporting identical bytes retains the first label and kind; it does not create a renamed duplicate or change its classification. `get_studio_asset` rechecks the file. `list_studio_assets` supports pages of up to 100 entries and an offset, so an agent can recover existing assets instead of importing them again.

A model classified as `character` is a library item. It is not automatically selected as the animation target and does not replace the existing registered character. [UniRig](unirig.md) can consume these registered mesh IDs and return reviewed rigged output to the library, but a new rig still requires an explicit compatible retarget setup.

Self-contained GLB source files can be displayed directly. For another supported format, **Build GLB preview** or `build_studio_asset_preview(asset_id, attempt)` explicitly starts a fresh background Blender process. It imports only that asset and exports a managed GLB, with a report and content hashes. `get_studio_asset_preview` only reads an existing result. A preview is a representation for inspection, not proof of good topology, weights, game-engine compatibility or material fidelity.

Preview attempts are numbered 0–99. Read an uncertain attempt before choosing a new one: a lost response does not establish that Blender never ran. Fresh background processes use a factory scene and do not take over the user's open Blender project.

## Build a reusable attachment scene

An immutable scene contains a title and 1–16 attachment rows. Each row selects an asset registered as a prop and defines:

| Control | Meaning |
| --- | --- |
| Bone | One of the registered 24 target bones, such as `RightHand`; `null` places the prop in world space. |
| `translation_m` | Three offsets in meters. Bone attachment uses the posed bone-head axes; world placement uses Blender Z-up coordinates. |
| `rotation_degrees` | Local XYZ Euler rotation, each axis between −360° and 360°. |
| `scale` | Positive XYZ scale, each component from 0.001 to 100. Negative or collapsed scale is rejected. |

Studio provides bone/world selection and numeric transform fields. The resulting model is shown in the interactive viewer with orbit, zoom, playback, frame scrubbing and bone visibility. Build the scene to inspect the actual attachment against the animation; changing form values alone does not run Blender or alter a saved result.

MCP example for `save_prop_scene`:

```json
{
  "request": {
    "title": "Right-hand sword",
    "attachments": [{
      "asset_id": "<64-character prop asset ID>",
      "bone": "RightHand",
      "translation_m": [0.0, 0.0, 0.0],
      "rotation_degrees": [0.0, 0.0, 90.0],
      "scale": [1.0, 1.0, 1.0]
    }]
  }
}
```

The returned `scene_id` identifies the complete placement recipe. Save a changed placement to obtain a new scene ID; the old recipe remains available through `get_prop_scene` and `list_prop_scenes`. The same asset can be used in multiple rows, for example two matching swords on different hands.

Props must be static meshes. A file with an armature or animation is rejected during scene construction. A hinged weapon, cloth accessory, procedural effect or separately animated object needs a different authored workflow; this feature makes rigid attachments.

## Select the exact character animation

Prepare an attachment build with `prepare_prop_scene_handoff(scene_id, source, attempt)`. The preparation binds the scene, model bytes, verified character animation, converter scripts and output attempt. It writes a request but does not execute Blender.

For a generated Kimodo motion, select the completed **character** handoff, including its exact original/variant source and character-export attempt:

```json
{
  "kind": "kimodo",
  "job_id": "<32-character completed generation job ID>",
  "sample_index": 0,
  "variant_id": null,
  "attempt": 0
}
```

For a cleaned and retargeted FreeMoCap animation, select its verified character handoff:

```json
{
  "kind": "capture_character",
  "capture_handoff_id": "<64-character completed character handoff ID>"
}
```

The first source's `attempt` selects an existing character export. The outer preparation `attempt` identifies the new prop-scene build; these are independent. A raw generation job, a capture landmark file or an unverified `.blend` path is not sufficient. Build and verify the character motion first using the relevant [editor handoff](editor-handoffs.md) or [capture retarget](capture-retarget.md) workflow.

Run `execute_prop_scene_handoff(handoff_id)` only after preparation. It opens the verified generated character file in a fresh background Blender process, imports the static props and samples their transforms at every animation frame. Bone attachments follow the selected posed bone; world props stay in world space. The character's per-frame bone transforms are hashed before and after assembly and must remain unchanged.

Read `get_prop_scene_handoff` until the result is verified completed. `list_prop_scene_handoffs` finds saved attempts after a browser or agent restart; opening one exact ID verifies its files. An uncertain dispatch is not automatically repeated. Changed sources or scripts require a new preparation identity rather than silently reusing an old receipt.

## Outputs and Unreal use

| File | Purpose |
| --- | --- |
| `scene.blend` | The character animation and rigid prop attachments in an editable Blender scene. |
| `scene-preview.glb` | Combined animated viewer export, with sampled prop transforms. |
| `prop_00.fbx`, subsequent numbered files | Separate static meshes suitable for a deliberate game-engine asset import. |
| `attachment-recipe.json` | Original bone/world placements, source identity and coordinate conventions. |

Use `get_prop_scene_artifact(handoff_id, path)` with a path listed in the completed receipt. Studio uses the same verification before serving previews or downloads. The build preserves the original character animation; it does not add finger curl, grip contacts, elbow IK, two-hand constraints or collision avoidance. Adjust the character pose separately when a grip requires it, then build props against the new verified character result.

Unreal sockets use the target skeleton's own coordinate conventions. The attachment recipe records Blender Z-up and bone-local transforms; its numeric offsets must not be pasted into Unreal as if the coordinate systems were identical. Import static props and create/review the intended sockets through the target Unreal editor, then check scale, orientation and grip during playback. This release does not automatically import props into Unreal or convert these placements into Unreal sockets. Character animation import remains the explicit verified [Unreal handoff](editor-handoffs.md#unreal).

## Verification scope

Backend tests cover immutable import, identity checks, preview/build receipts, source selection, transform bounds and changed-file rejection. Fresh Blender verification has also exercised attachments on a synthetic capture-character motion and an existing generated motion. Those checks establish preservation of the tested 24-bone character animation and attachment transforms. They do not establish that arbitrary assets are production ready, that fingers grip a weapon naturally or that Unreal accepted the exported meshes.
