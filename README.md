# PSO Ultimate Importer
Addon for Blender 4.2+ / 5.x that imports 3D model formats used by Phantasy Star Online and its various versions.

This addon is based on the format reverse-engineering work by Benjamin Collins (Kion). Check out his work on [GitLab](https://gitlab.com/dashgl/ikaruga/-/snippets), [YouTube](https://www.youtube.com/@kion_dgl). and [DashGL](https://dashgl.org/).

Need help with this addon? Join the [Realitea Discord](https://discord.gg/43ggeGC8A8) and ask for help.

<img width="930" height="556" alt="promo" src="https://github.com/user-attachments/assets/4fbb585a-949a-44f1-a02d-1d08fe3fc384" />

## Features
Currently, the addon supports the following formats:

| Platform      | Actors | Scenes | Textures | Archives |
|---------------|-------------|------------------|---------------|----------|
| Dreamcast v2  | `.nj`       | `n.rel`           | `.pvm`        |         |
| GameCube      | `.gj`       | `n.rel`           | `.gvm`        |         |
| Blue Burst    | `.xj`       | `n.rel`           | `.xvm`        |`.bml`   |

Import settings allow you to customize some quality-of-life features.
- **Blend Vertex Colors** - recreates the original lighting from PSO by blending the vertex colors onto albedo textures, for any mesh that has vertex colors
- **Disable Color Correction** - adjusts the Color Management settings in your scene to sRGB / Standard, so colors match the original colors from PSO
- **Extend Viewport Clip Distance** - increases the clipping distance in your scene just enough so that the model is immediately visible, no matter how large it might be

## Installation
1. Click the green "Code" button above and press "Download ZIP"
2. Go into Blender's addon preferences (File → Preferences → Addons)
3. Click the <img width="20" height="21" alt="image" src="https://github.com/user-attachments/assets/92cefcff-c9d0-4c29-b1ef-a7efe9d07016" /> button on the top right of the window, and select "Install from Disk..."
4. Browse to the ZIP file you just downloaded, select it, and press Return/Enter.
   
You can find the importers via Blender's "_FIle_" -> "_Import_" menu.

## Previews

<img width="589" height="364" alt="promo2" src="https://github.com/user-attachments/assets/765c1ae4-75cd-43b5-815c-dd2554868452" />

Support for importing .NJ and .XJ actor models

<img width="834" height="482" alt="promo3" src="https://github.com/user-attachments/assets/028c9a3e-674c-4142-a0b9-2a725410a091" />

Support for importing GameCube (Ep1&2 and Ep3) .REL scenes and textures.

<img width="575" height="429" alt="promo4" src="https://github.com/user-attachments/assets/57a6daac-cdf0-4da1-88bf-abda3f1443de" />

Support for importing Blue Burst / Episode 4 models and textures, including DXT1 / DXT3 / DXT5 compression types
