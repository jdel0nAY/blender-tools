# blender_tools

Public collection of Blender add-ons and scripts by Jean Delaunay.

Each tool lives in its own subfolder and is a self-contained add-on: install
the tool's `.py` via **Edit → Preferences → Add-ons → Install…**, then see that
tool's own README for usage.

## Tools

| Tool | Description | Version |
|------|-------------|---------|
| [jd_ShotInfoPanel_forBlender](jd_ShotInfoPanel_forBlender/) | Pulls per-shot data (thumbnail, camera focal length, frame range, fps, resolution) from a Google Sheet into the viewport sidebar, with a playblast HUD and camera reference-image setup. | 1.0.0 |

<!-- Add new tools as rows above. -->

## Naming & versioning

Tools follow `jd_<ToolName>_forBlender`, and each build's file carries a version
suffix using underscores (e.g. `_v1_0_0.py`) — underscores because a Blender
add-on's filename becomes its Python module name, where dots and hyphens are
invalid.

## License

[MIT](LICENSE) — applies to everything in this repository unless a subfolder
states otherwise.
