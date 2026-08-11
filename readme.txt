========================================================================
ComfyUI MiniMax Hailuo H3 Custom Loop Controller
========================================================================

- Tested & compatible with ComfyUI v0.31.0.
- All commands are formatted for Windows (PowerShell / Command Prompt).
- Recommended: Output resolution (--width / --height) must be a multiple of 32 (e.g., 864x480).

For full documentation and visual architecture diagrams, see README.md.

------------------------------------------------------------------------
1. CUSTOM NODE INSTALLATION
------------------------------------------------------------------------
Copy __init__.py into your ComfyUI custom nodes directory:

ComfyUI/
├── custom_nodes/
│   └── LoopLastFrame/
│       └── __init__.py
├── models/
├── input/
├── output/
└── main.py


------------------------------------------------------------------------
2. PROMPT FILE STRUCTURE (prompts.txt)
------------------------------------------------------------------------
The text prompt file can contain a GLOBAL prefix and individual scene loops:

GLOBAL:
Same character identity, same face, same hair, same clothing and same environment geometry throughout the entire sequence. Cinematic photorealistic style.

---LOOP---
The character slowly walks toward the ancient stone doorway while looking ahead with determination.

---LOOP---
The character reaches the doorway and slowly opens it, revealing a mysterious blue light behind it.

---LOOP---
The character steps through the doorway and looks around cautiously as dust moves through the air.

---LOOP---
The character suddenly hears a deep rumble and turns toward the source of the sound.


------------------------------------------------------------------------
3. COMMON COMMAND EXAMPLES (WINDOWS)
------------------------------------------------------------------------

# Basic run (all prompts):
python loop.py --workflow .\MiniMax_H3_Loop_API.json --prompt .\prompts.txt --image .\initial.jpg

# Run specific number of loops (e.g. 8 loops):
python loop.py --workflow .\MiniMax_H3_Loop_API.json --prompt .\prompts.txt --image .\initial.jpg --loops 8

# Run with custom video dimensions (multiples of 32) and duration:
python loop.py --workflow .\MiniMax_H3_Loop_API.json --prompt .\prompts.txt --image .\initial.jpg --loops 8 --width 864 --height 480 --duration 5

# Resume from loop 4 using saved last frame of loop 3:
python loop.py --workflow .\MiniMax_H3_Loop_API.json --prompt .\prompts.txt --image .\h3_loop_output\loop_0003_last.png --start-loop 4 --pause 30

# Workflow structural check (dry run):
python loop.py --workflow .\MiniMax_H3_Loop_API.json --prompt .\prompts.txt --image .\initial.jpg --check
