import argparse
import gc
import json
import os
import subprocess
import threading
import sys
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
import random

import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_BASE_URL = "http://127.0.0.1:8188"

PROMPT_NODE = "15"
IMAGE_NODE = "53"
LAST_FRAME_NODE = "92"
LAST_FRAME_IMAGE_NODE = "93"
PREVIEW_NODE = "56"
SAVE_VIDEO_NODE = "85"
SAVE_AUDIO_NODE = "94"

# Workflow parameter nodes
WIDTH_NODE = "69"
HEIGHT_NODE = "70"
DURATION_NODE = "18"

# Safety / cleanup thresholds
VRAM_LIMIT_GB = 1.0
RAM_LIMIT_GB = 16.0
GPU_TEMP_LIMIT_C = 68.0

# Additional cooldown after memory thresholds are reached
ADDITIONAL_COOLDOWN_SECONDS = 30

# Generation monitoring interval
MONITOR_INTERVAL_SECONDS = 5

# How frequently to check memory/temperature while waiting
RESOURCE_CHECK_INTERVAL = 2

# Maximum time to wait for cleanup conditions
RESOURCE_WAIT_TIMEOUT = 600

# Audio / final assembly
AUDIO_SAMPLE_RATE = 44100
AUDIO_CROSSFADE_MS = 40
FFMPEG_BINARY = "ffmpeg"


# ============================================================================
# PROMPTS
# ============================================================================

def split_prompts(text):
    """
    Supported separators:

        ---LOOP---

    or:

        ===LOOP===
    """

    text = text.replace("\r\n", "\n")

    parts = text.split("---LOOP---")

    if len(parts) == 1:
        parts = text.split("===LOOP===")

    prompts = [
        p.strip()
        for p in parts
        if p.strip()
    ]

    if not prompts:
        raise RuntimeError(
            "No prompts found in prompts.txt."
        )

    return prompts


# ============================================================================
# WORKFLOW
# ============================================================================

def load_api_workflow(path):
    data = json.loads(
        Path(path).read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(data, dict):
        raise RuntimeError(
            "Workflow JSON must be an API-format object."
        )

    if PROMPT_NODE not in data:
        raise RuntimeError(
            f"Node {PROMPT_NODE} "
            "(MiniMaxH3ImageToVideo) was not found."
        )

    if data[PROMPT_NODE].get("class_type") != "MiniMaxH3ImageToVideo":
        raise RuntimeError(
            f"Node {PROMPT_NODE} is "
            f"{data[PROMPT_NODE].get('class_type')}, "
            "not MiniMaxH3ImageToVideo."
        )

    if IMAGE_NODE not in data:
        raise RuntimeError(
            f"Node {IMAGE_NODE} was not found."
        )

    if data[IMAGE_NODE].get("class_type") != "LoadImage":
        raise RuntimeError(
            f"Node {IMAGE_NODE} must be LoadImage."
        )

    if PREVIEW_NODE not in data:
        raise RuntimeError(
            f"Node {PREVIEW_NODE} was not found."
        )

    if data[PREVIEW_NODE].get("class_type") != "PreviewImage":
        raise RuntimeError(
            f"Node {PREVIEW_NODE} must be PreviewImage."
        )

    if "35" not in data:
        raise RuntimeError(
            "Node 35 (VAEDecode) was not found."
        )

    if data["35"].get("class_type") != "VAEDecode":
        raise RuntimeError(
            "Node 35 must be VAEDecode."
        )
    if SAVE_AUDIO_NODE not in data:
        raise RuntimeError(
            f"Node {SAVE_AUDIO_NODE} (SaveAudio) was not found."
        )

    if data[SAVE_AUDIO_NODE].get("class_type") != "SaveAudio":
        raise RuntimeError(
            f"Node {SAVE_AUDIO_NODE} must be SaveAudio."
        )

    # Verify parameter nodes exist.
    for node_id, description in (
        (WIDTH_NODE, "width"),
        (HEIGHT_NODE, "height"),
        (DURATION_NODE, "duration"),
    ):
        if node_id not in data:
            raise RuntimeError(
                f"Node {node_id} ({description}) "
                "was not found in workflow."
            )

    return data


def set_random_seed(wf, seed):
    """
    Find RandomNoise in an API-format ComfyUI workflow
    and set its noise_seed.

    The node is detected by class_type, not by a fixed node ID.
    """

    for node_id, node in wf.items():

        if not isinstance(node, dict):
            continue

        if node.get("class_type") == "RandomNoise":

            node.setdefault(
                "inputs",
                {}
            )

            node["inputs"]["noise_seed"] = int(seed)

            return str(node_id), int(seed)

    raise RuntimeError(
        "No RandomNoise node found in API workflow."
    )


def set_video_parameters(
    wf,
    width=None,
    height=None,
    duration=None,
):
    """
    Modify video resolution and duration.

    Width:
        Node 69

    Height:
        Node 70

    Duration:
        Node 18

    If a parameter is None, the existing workflow
    value is preserved.
    """

    # ------------------------------------------------------------------------
    # WIDTH
    # ------------------------------------------------------------------------

    if width is not None:

        if WIDTH_NODE not in wf:
            raise RuntimeError(
                f"Width node {WIDTH_NODE} "
                "not found in workflow."
            )

        node = wf[WIDTH_NODE]

        node.setdefault(
            "widgets_values",
            []
        )

        if not node["widgets_values"]:
            node["widgets_values"].append(
                int(width)
            )
        else:
            node["widgets_values"][0] = int(width)

        # Also support workflows where the value
        # is represented inside inputs.
        if "inputs" in node:
            if "value" in node["inputs"]:
                node["inputs"]["value"] = int(width)

    # ------------------------------------------------------------------------
    # HEIGHT
    # ------------------------------------------------------------------------

    if height is not None:

        if HEIGHT_NODE not in wf:
            raise RuntimeError(
                f"Height node {HEIGHT_NODE} "
                "not found in workflow."
            )

        node = wf[HEIGHT_NODE]

        node.setdefault(
            "widgets_values",
            []
        )

        if not node["widgets_values"]:
            node["widgets_values"].append(
                int(height)
            )
        else:
            node["widgets_values"][0] = int(height)

        if "inputs" in node:
            if "value" in node["inputs"]:
                node["inputs"]["value"] = int(height)

    # ------------------------------------------------------------------------
    # DURATION
    # ------------------------------------------------------------------------

    if duration is not None:

        if DURATION_NODE not in wf:
            raise RuntimeError(
                f"Duration node {DURATION_NODE} "
                "not found in workflow."
            )

        node = wf[DURATION_NODE]

        node.setdefault(
            "widgets_values",
            []
        )

        duration_value = str(duration)

        if not node["widgets_values"]:
            node["widgets_values"].append(
                duration_value
            )
        else:
            node["widgets_values"][0] = duration_value

        if "inputs" in node:
            if "value" in node["inputs"]:
                node["inputs"]["value"] = float(duration)

    return wf


def get_workflow_video_parameters(wf):
    """
    Return the effective width, height and duration
    values stored in the workflow nodes.
    """

    width = None
    height = None
    duration = None

    if WIDTH_NODE in wf:

        values = wf[WIDTH_NODE].get(
            "widgets_values",
            []
        )

        if values:
            width = values[0]

    if HEIGHT_NODE in wf:

        values = wf[HEIGHT_NODE].get(
            "widgets_values",
            []
        )

        if values:
            height = values[0]

    if DURATION_NODE in wf:

        values = wf[DURATION_NODE].get(
            "widgets_values",
            []
        )

        if values:
            duration = values[0]

    return width, height, duration


def modify_workflow(
    workflow,
    prompt,
    uploaded_filename,
    loop_index,
    width=None,
    height=None,
    duration=None,
    uploaded_last_filename=None,
):
    wf = json.loads(
        json.dumps(workflow)
    )

    # ------------------------------------------------------------------------
    # Node 15: prompt
    # ------------------------------------------------------------------------

    wf[PROMPT_NODE]["inputs"]["prompt"] = prompt

    # ------------------------------------------------------------------------
    # Node 53:
    # Previous loop final frame becomes current first frame
    # ------------------------------------------------------------------------

    wf[IMAGE_NODE]["inputs"]["image"] = uploaded_filename

    # ------------------------------------------------------------------------
    # LAST-FRAME TARGET SAFETY
    #
    # A generated last frame is NOT the same thing as a requested last-frame
    # target. Unless --last-frame was explicitly supplied, remove the H3
    # last_frame input AND the target-image nodes from the payload.
    #
    # This is deliberately enforced on EVERY loop, including loop 1.
    # ------------------------------------------------------------------------

    h3_inputs = wf[PROMPT_NODE]["inputs"]

    if uploaded_last_filename:
        # --last-frame was explicitly requested.
        if LAST_FRAME_IMAGE_NODE not in wf:
            raise RuntimeError(
                f"--last-frame was supplied, but node "
                f"{LAST_FRAME_IMAGE_NODE} is missing from the workflow."
            )

        if LAST_FRAME_NODE not in wf:
            raise RuntimeError(
                f"--last-frame was supplied, but node "
                f"{LAST_FRAME_NODE} is missing from the workflow."
            )

        wf[LAST_FRAME_IMAGE_NODE]["inputs"]["image"] = (
            uploaded_last_filename
        )

        h3_inputs["last_frame"] = [
            LAST_FRAME_NODE,
            0,
        ]

    else:
        # NO --last-frame: absolutely no fixed last-frame target.
        h3_inputs.pop("last_frame", None)

        # Remove the target nodes from this request payload as an additional
        # safety measure. They cannot accidentally participate in execution.
        wf.pop(LAST_FRAME_NODE, None)
        wf.pop(LAST_FRAME_IMAGE_NODE, None)

    # ------------------------------------------------------------------------
    # HARD VALIDATION BEFORE SUBMISSION
    # ------------------------------------------------------------------------

    has_last_frame = "last_frame" in h3_inputs
    has_target_nodes = (
        LAST_FRAME_NODE in wf or
        LAST_FRAME_IMAGE_NODE in wf
    )

    if not uploaded_last_filename:
        if has_last_frame or has_target_nodes:
            raise RuntimeError(
                "SAFETY ERROR: a last-frame target is still present in "
                "the workflow even though --last-frame was NOT supplied."
            )
    else:
        if not has_last_frame:
            raise RuntimeError(
                "SAFETY ERROR: --last-frame was supplied but node 15 "
                "does not contain a last_frame input."
            )

    # ------------------------------------------------------------------------
    # Node 56:
    # Capture generated frames from VAEDecode 35
    # ------------------------------------------------------------------------

    wf[PREVIEW_NODE]["inputs"]["images"] = [
        "35",
        0,
    ]

    # ------------------------------------------------------------------------
    # Random seed for this loop
    # ------------------------------------------------------------------------

    seed = random.randint(
        0,
        1125899906842624
    )

    random_node_id, actual_seed = set_random_seed(
        wf,
        seed
    )

    log_event(
        f"RANDOM SEED: {actual_seed} | "
        f"RandomNoise node: {random_node_id}"
    )

    # ------------------------------------------------------------------------
    # Video parameters
    # ------------------------------------------------------------------------

    set_video_parameters(
        wf,
        width=width,
        height=height,
        duration=duration,
    )

    actual_width, actual_height, actual_duration = (
        get_workflow_video_parameters(wf)
    )

    log_event(
        f"VIDEO PARAMETERS | "
        f"resolution={actual_width}x{actual_height} | "
        f"duration={actual_duration}s"
    )

    # ------------------------------------------------------------------------
    # TEMPORAL LOGIC
    #
    # The generated previous last frame is handled by IMAGE_NODE (first_frame)
    # on the next loop. It must NEVER be promoted to H3's last_frame target.
    #
    # The ONLY exception is an explicit --last-frame request, represented by
    # uploaded_last_filename. That target is intentionally allowed above.
    #
    # This applies identically to loop 1 and loop 2+.
    # ------------------------------------------------------------------------

    return wf


def print_workflow_check(workflow):

    print("=" * 70)

    log_event(
        "RUN FINISHED SUCCESSFULLY"
    )

    print(
        "MiniMax H3 API Workflow Check"
    )

    print("=" * 70)

    checks = {
        PROMPT_NODE: "MiniMaxH3ImageToVideo",
        IMAGE_NODE: "LoadImage",
        PREVIEW_NODE: "PreviewImage",
        "57": "ImageResizeKJv2",
        SAVE_VIDEO_NODE: "SaveVideo",
        SAVE_AUDIO_NODE: "SaveAudio",
    }

    for node_id, expected in checks.items():

        node = workflow.get(node_id)

        actual = (
            node.get("class_type")
            if node
            else None
        )

        ok = actual == expected

        print(
            f"{'OK' if ok else 'FAIL'}  "
            f"node {node_id}: "
            f"{actual!r} "
            f"(expected {expected!r})"
        )

    h3 = workflow[PROMPT_NODE]["inputs"]

    width, height, duration = (
        get_workflow_video_parameters(
            workflow
        )
    )

    print()

    print(
        "Prompt input:        "
        f"node {PROMPT_NODE} / inputs.prompt"
    )

    print(
        f"First-frame source:  "
        f"{h3.get('first_frame')}"
    )

    print(
        f"Last-frame source:   "
        f"{h3.get('last_frame')}"
    )

    print(
        f"Node {IMAGE_NODE} image:       "
        f"{workflow[IMAGE_NODE]['inputs'].get('image')}"
    )

    print(
        f"Preview output:      "
        f"node {PREVIEW_NODE} "
        f"<- generated frames node 35"
    )
    print(
        f"Audio output:        node {SAVE_AUDIO_NODE} "
        f"<- decoded audio node 36"
    )

    print(
        f"Width:               "
        f"node {WIDTH_NODE} -> {width}"
    )

    print(
        f"Height:              "
        f"node {HEIGHT_NODE} -> {height}"
    )

    print(
        f"Duration:            "
        f"node {DURATION_NODE} -> {duration}s"
    )

    print("=" * 70)


# ============================================================================
# COMFYUI HTTP
# ============================================================================

def upload_image(
    base_url,
    image_path,
    filename=None,
):

    image_path = Path(image_path)

    if not image_path.exists():
        raise FileNotFoundError(
            image_path
        )

    if filename is None:
        filename = image_path.name

    mime = (
        "image/png"
        if image_path.suffix.lower() == ".png"
        else "image/jpeg"
    )

    with image_path.open("rb") as f:

        files = {
            "image": (
                filename,
                f,
                mime,
            )
        }

        data = {
            "overwrite": "true",
            "type": "input",
            "subfolder": "",
        }

        r = requests.post(
            base_url.rstrip("/")
            + "/upload/image",
            files=files,
            data=data,
            timeout=120,
        )

    if not r.ok:

        raise RuntimeError(
            f"Image upload failed: "
            f"HTTP {r.status_code}\n"
            f"{r.text}"
        )

    result = r.json()

    return (
        result["name"],
        result.get(
            "subfolder",
            ""
        ),
        result.get(
            "type",
            "input"
        ),
    )


def queue_prompt(
    base_url,
    workflow,
):

    client_id = str(
        uuid.uuid4()
    )

    payload = {
        "prompt": workflow,
        "client_id": client_id,
    }

    body = json.dumps(
        payload
    ).encode("utf-8")

    req = urllib.request.Request(
        base_url.rstrip("/")
        + "/prompt",
        data=body,
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=120
        ) as response:

            raw = (
                response
                .read()
                .decode("utf-8")
            )

    except urllib.error.HTTPError as e:

        error_body = (
            e.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        raise RuntimeError(
            f"ComfyUI rejected the workflow: "
            f"HTTP {e.code}\n"
            f"{error_body}"
        ) from e

    result = json.loads(
        raw
    )

    if "prompt_id" not in result:

        raise RuntimeError(
            f"Unexpected /prompt response:\n"
            f"{raw}"
        )

    return result["prompt_id"]


def wait_for_history(
    base_url,
    prompt_id,
    poll=1.0,
    timeout=3600,
):

    url = (
        base_url.rstrip("/")
        + "/history/"
        + urllib.parse.quote(
            prompt_id
        )
    )

    start = time.time()

    while True:

        if time.time() - start > timeout:

            raise TimeoutError(
                f"Timed out waiting for "
                f"prompt {prompt_id}"
            )

        try:

            r = requests.get(
                url,
                timeout=30,
            )

            r.raise_for_status()

            history = r.json()

        except requests.RequestException:

            time.sleep(poll)

            continue

        if prompt_id in history:

            item = history[prompt_id]

            status = item.get(
                "status",
                {}
            )

            if status.get(
                "status_str"
            ) == "error":

                messages = status.get(
                    "messages",
                    [],
                )

                raise RuntimeError(
                    "ComfyUI reported an "
                    "execution error:\n"
                    + json.dumps(
                        messages,
                        indent=2,
                    )
                )

            if item.get("outputs"):

                return item

        time.sleep(
            poll
        )


def find_preview_images(
    history_item,
    preview_node=PREVIEW_NODE,
):

    outputs = history_item.get(
        "outputs",
        {}
    )

    node = outputs.get(
        str(preview_node),
        {}
    )

    images = node.get(
        "images",
        []
    )

    if not images:

        for node_output in outputs.values():

            candidates = (
                node_output.get(
                    "images",
                    []
                )
            )

            if candidates:

                images.extend(
                    candidates
                )

    if not images:

        raise RuntimeError(
            "No image output was found "
            "in ComfyUI history. "
            f"Expected PreviewImage node "
            f"{preview_node}."
        )

    return images


def download_comfy_image(
    base_url,
    image_info,
    destination,
):

    params = {
        "filename":
            image_info["filename"],

        "subfolder":
            image_info.get(
                "subfolder",
                ""
            ),

        "type":
            image_info.get(
                "type",
                "output"
            ),
    }

    r = requests.get(
        base_url.rstrip("/")
        + "/view",
        params=params,
        timeout=120,
    )

    if not r.ok:

        raise RuntimeError(
            f"Could not download generated "
            f"frame: HTTP {r.status_code}\n"
            f"{r.text}"
        )

    destination = Path(
        destination
    )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Write immediately.
    destination.write_bytes(
        r.content
    )

    return destination


def _normalize_media_entries(value):
    """Normalize ComfyUI media output metadata to a list of dicts."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def find_audio_outputs(history_item, audio_node=SAVE_AUDIO_NODE):
    """Find audio files emitted by the SaveAudio node in ComfyUI history."""
    outputs = history_item.get("outputs", {})
    node = outputs.get(str(audio_node), {})

    candidates = []
    for key in ("audio", "audios", "files"):
        candidates.extend(_normalize_media_entries(node.get(key)))

    # Some ComfyUI versions expose UI metadata under nested structures.
    if not candidates:
        def walk(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in ("audio", "audios"):
                        candidates.extend(_normalize_media_entries(v))
                    else:
                        walk(v)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(node)

    # Keep only entries that can be retrieved through /view.
    valid = [x for x in candidates if x.get("filename")]
    if not valid:
        raise RuntimeError(
            f"No audio output was found in ComfyUI history. "
            f"Expected SaveAudio node {audio_node}. "
            f"Available node output keys: {list(node.keys())}"
        )
    return valid


def download_comfy_media(base_url, media_info, destination):
    """Download an output media file using ComfyUI's /view endpoint."""
    params = {
        "filename": media_info["filename"],
        "subfolder": media_info.get("subfolder", ""),
        "type": media_info.get("type", "output"),
    }

    r = requests.get(
        base_url.rstrip("/") + "/view",
        params=params,
        timeout=120,
    )
    if not r.ok:
        raise RuntimeError(
            f"Could not download generated media: HTTP {r.status_code}\\n{r.text}"
        )

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(r.content)
    return destination


def _run_ffmpeg(args, description):
    """Run ffmpeg and raise a useful error on failure."""
    cmd = [FFMPEG_BINARY, "-hide_banner", "-loglevel", "error", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg was not found in PATH. Install ffmpeg and restart the shell."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed while {description}:\\n{result.stderr.strip()}"
        )


def build_master_audio(outdir, end_loop):
    """Create a single audio spine with short crossfades between loop audio files."""
    audio_files = [
        Path(outdir) / f"loop_{i:04d}_audio.flac"
        for i in range(1, end_loop + 1)
    ]
    missing = [p for p in audio_files if not p.exists()]
    if missing:
        raise RuntimeError(
            "Cannot build master audio; missing files:\\n" +
            "\\n".join(str(p) for p in missing)
        )

    if len(audio_files) == 1:
        master = Path(outdir) / "master_audio.flac"
        _run_ffmpeg(
            ["-y", "-i", str(audio_files[0]), "-c:a", "flac", str(master)],
            "writing master audio",
        )
        return master

    # Acrossfade keeps the seam continuous without creating a long overlap.
    # 40 ms is intentionally small so spoken dialogue is not noticeably doubled.
    current = audio_files[0]
    for i, next_audio in enumerate(audio_files[1:], start=2):
        output = Path(outdir) / f".audio_spine_{i:04d}.flac"
        duration = AUDIO_CROSSFADE_MS / 1000.0
        _run_ffmpeg(
            [
                "-y",
                "-i", str(current),
                "-i", str(next_audio),
                "-filter_complex", f"[0:a][1:a]acrossfade=d={duration:.3f}:c1=tri:c2=tri[a]",
                "-map", "[a]",
                "-c:a", "flac",
                str(output),
            ],
            f"joining audio loop {i-1} -> {i}",
        )
        if current.name.startswith(".audio_spine_"):
            try:
                current.unlink()
            except OSError:
                pass
        current = output

    master = Path(outdir) / "master_audio.flac"
    if master.exists():
        master.unlink()
    current.replace(master)
    return master


def build_master_video(outdir, end_loop, master_audio):
    """Concatenate generated MP4s and mux the continuous audio spine."""
    video_files = [
        Path(outdir) / f"loop_{i:04d}.mp4"
        for i in range(1, end_loop + 1)
    ]
    missing = [p for p in video_files if not p.exists()]
    if missing:
        # The current workflow may use .webm/.mkv depending on SaveVideo auto codec.
        # Fall back to whatever per-loop video file was recorded below.
        video_files = sorted(Path(outdir).glob("loop_*.mp4"))
        if len(video_files) < end_loop:
            raise RuntimeError(
                "Cannot build master video; per-loop MP4 files were not found."
            )

    concat_file = Path(outdir) / "video_concat.txt"
    concat_file.write_text(
        "\\n".join(f"file '{p.resolve().as_posix().replace(chr(39), chr(39)+chr(39))}'" for p in video_files),
        encoding="utf-8",
    )

    joined = Path(outdir) / ".video_joined.mkv"
    master = Path(outdir) / "master_loop.mp4"

    _run_ffmpeg(
        [
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-map", "0:v:0",
            "-an",
            "-c:v", "copy",
            str(joined),
        ],
        "concatenating loop videos",
    )

    _run_ffmpeg(
        [
            "-y",
            "-i", str(joined),
            "-i", str(master_audio),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            "-shortest",
            str(master),
        ],
        "muxing master video and continuous audio",
    )

    try:
        joined.unlink()
        concat_file.unlink()
    except OSError:
        pass

    return master


# ============================================================================
# LOGGING
# ============================================================================

LOG_FILE = (
    Path(__file__).resolve().parent
    / "loop.log"
)


class TeeLogger:

    def __init__(self, path):

        self.path = Path(path)

        self.fp = self.path.open(
            "a",
            encoding="utf-8",
            buffering=1,
        )

    def write(self, text):

        try:

            sys.__stdout__.write(
                text
            )

            sys.__stdout__.flush()

        except Exception:
            pass

        if text:

            try:

                self.fp.write(
                    text
                )

                self.fp.flush()

            except Exception:
                pass

    def flush(self):

        try:
            sys.__stdout__.flush()
        except Exception:
            pass

        try:
            self.fp.flush()
        except Exception:
            pass

    def close(self):

        try:

            self.fp.flush()
            self.fp.close()

        except Exception:
            pass


def log_event(message):

    timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"{timestamp} | {message}",
        flush=True,
    )


def setup_logging():

    global LOG_FILE

    logger = TeeLogger(
        LOG_FILE
    )

    sys.stdout = logger
    sys.stderr = logger

    print()

    print("=" * 80)

    print(
        f"LOG FILE: {LOG_FILE}"
    )

    log_event(
        "LOGGING INITIALIZED"
    )

    print(
        f"START TIME: "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print("=" * 80)

    return logger


# ============================================================================
# SYSTEM MONITORING
# ============================================================================

def get_system_stats():

    stats = {

        "ram_used_gb": None,
        "ram_total_gb": None,
        "ram_percent": None,

        "vram_used_gb": None,
        "vram_total_gb": None,

        "gpu_temp_c": None,
        "gpu_util": None,
        "gpu_power_w": None,
        "gpu_clock_mhz": None,

        "cpu_percent": None,
        "cpu_temp_c": None,
    }

    # ------------------------------------------------------------------------
    # RAM + CPU
    # ------------------------------------------------------------------------

    try:

        ps = (
            "$os=Get-CimInstance "
            "Win32_OperatingSystem; "

            "$cpu=(Get-Counter "
            "'\\Processor(_Total)\\% Processor Time'"
            ").CounterSamples.CookedValue; "

            "[PSCustomObject]@{"

            "TotalGB=[math]::Round("
            "$os.TotalVisibleMemorySize/1MB,2"
            ");"

            "FreeGB=[math]::Round("
            "$os.FreePhysicalMemory/1MB,2"
            ");"

            "CPU=[math]::Round($cpu,1)"

            "} | ConvertTo-Json -Compress"
        )

        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if (
            result.returncode == 0
            and result.stdout.strip()
        ):

            data = json.loads(
                result.stdout
            )

            total = float(
                data["TotalGB"]
            )

            free = float(
                data["FreeGB"]
            )

            used = max(
                0.0,
                total - free,
            )

            stats[
                "ram_total_gb"
            ] = total

            stats[
                "ram_used_gb"
            ] = used

            if total > 0:

                stats[
                    "ram_percent"
                ] = (
                    used
                    / total
                    * 100
                )

            stats[
                "cpu_percent"
            ] = float(
                data["CPU"]
            )

    except Exception:
        pass

    # ------------------------------------------------------------------------
    # NVIDIA GPU
    # ------------------------------------------------------------------------

    try:

        query = (
            "utilization.gpu,"
            "memory.used,"
            "memory.total,"
            "temperature.gpu,"
            "power.draw,"
            "clocks.gr"
        )

        result = subprocess.run(
            [
                "nvidia-smi",

                f"--query-gpu={query}",

                "--format="
                "csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if (
            result.returncode == 0
            and result.stdout.strip()
        ):

            row = (
                result.stdout
                .strip()
                .splitlines()[0]
            )

            values = [
                x.strip()
                for x in row.split(",")
            ]

            if len(values) >= 6:

                stats[
                    "gpu_util"
                ] = float(
                    values[0]
                )

                stats[
                    "vram_used_gb"
                ] = (
                    float(values[1])
                    / 1024
                )

                stats[
                    "vram_total_gb"
                ] = (
                    float(values[2])
                    / 1024
                )

                stats[
                    "gpu_temp_c"
                ] = float(
                    values[3]
                )

                stats[
                    "gpu_power_w"
                ] = float(
                    values[4]
                )

                stats[
                    "gpu_clock_mhz"
                ] = float(
                    values[5]
                )

    except Exception:
        pass

    # ------------------------------------------------------------------------
    # Optional CPU temperature
    # LibreHardwareMonitor
    # ------------------------------------------------------------------------

    try:

        ps = (
            "Get-CimInstance "
            "-Namespace "
            "root/LibreHardwareMonitor "
            "-ClassName Sensor "
            "-ErrorAction Stop | "

            "Where-Object {"
            "$_.SensorType -eq 'Temperature' "
            "-and $_.Name -match 'CPU|Package'"
            "} | "

            "Select-Object -First 1 "
            "-ExpandProperty Value"
        )

        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )

        if (
            result.returncode == 0
            and result.stdout.strip()
        ):

            stats[
                "cpu_temp_c"
            ] = float(
                result.stdout
                .strip()
                .splitlines()[0]
            )

    except Exception:
        pass

    return stats


def format_stats(stats):

    # RAM

    if (
        stats["ram_used_gb"]
        is not None

        and

        stats["ram_total_gb"]
        is not None
    ):

        ram = (
            f"{stats['ram_used_gb']:.1f}/"
            f"{stats['ram_total_gb']:.1f} GB "
            f"({stats['ram_percent']:.0f}%)"
        )

    else:

        ram = "N/A"

    # VRAM

    if (
        stats["vram_used_gb"]
        is not None

        and

        stats["vram_total_gb"]
        is not None
    ):

        vram = (
            f"{stats['vram_used_gb']:.2f}/"
            f"{stats['vram_total_gb']:.2f} GB"
        )

    else:

        vram = "N/A"

    # GPU temperature

    if stats["gpu_temp_c"] is not None:

        gpu_temp = (
            f"{stats['gpu_temp_c']:.0f} C"
        )

    else:

        gpu_temp = "N/A"

    return (
        f"RAM {ram} | "
        f"VRAM {vram} | "
        f"GPU TEMP {gpu_temp}"
    )


def print_system_stats(
    label="SYSTEM"
):

    stats = get_system_stats()

    print(
        f"[{label}] "
        f"{format_stats(stats)}"
    )

    return stats


# ============================================================================
# GENERATION MONITOR
# ============================================================================

class GenerationMonitor:

    """
    Monitor CPU/GPU/RAM/VRAM/power
    every 5 seconds during generation.
    """

    def __init__(
        self,
        interval=MONITOR_INTERVAL_SECONDS,
        log_prefix="MONITOR",
    ):

        self.interval = interval
        self.log_prefix = log_prefix

        self.stop_event = (
            threading.Event()
        )

        self.thread = None

    def start(self):

        self.stop_event.clear()

        self.thread = threading.Thread(
            target=self._run,
            name="generation-monitor",
            daemon=True,
        )

        self.thread.start()

        log_event(
            f"{self.log_prefix} STARTED | "
            f"interval={self.interval}s"
        )

    def stop(self):

        self.stop_event.set()

        if self.thread is not None:

            self.thread.join(
                timeout=self.interval + 2
            )

        log_event(
            f"{self.log_prefix} STOPPED"
        )

    def _run(self):

        while not self.stop_event.wait(
            self.interval
        ):

            self._log_sample()

    def _log_sample(self):

        stats = get_system_stats()

        ram_used = (
            stats["ram_used_gb"]
        )

        ram_total = (
            stats["ram_total_gb"]
        )

        ram_pct = (
            stats["ram_percent"]
        )

        cpu = (
            stats["cpu_percent"]
        )

        cpu_temp = (
            stats["cpu_temp_c"]
        )

        gpu = (
            stats["gpu_util"]
        )

        vram_used = (
            stats["vram_used_gb"]
        )

        vram_total = (
            stats["vram_total_gb"]
        )

        gpu_temp = (
            stats["gpu_temp_c"]
        )

        gpu_power = (
            stats["gpu_power_w"]
        )

        gpu_clock = (
            stats["gpu_clock_mhz"]
        )

        ram_text = (

            f"{ram_used:.1f}/"
            f"{ram_total:.1f} GB "
            f"({ram_pct:.0f}%)"

            if ram_used is not None

            else "N/A"
        )

        cpu_text = (

            f"{cpu:.0f}%"

            if cpu is not None

            else "N/A"
        )

        cpu_temp_text = (

            f" | CPU TEMP "
            f"{cpu_temp:.0f} C"

            if cpu_temp is not None

            else ""
        )

        gpu_text = (

            f"{gpu:.0f}%"

            if gpu is not None

            else "N/A"
        )

        vram_text = (

            f"{vram_used:.2f}/"
            f"{vram_total:.2f} GB"

            if vram_used is not None

            else "N/A"
        )

        temp_text = (

            f"{gpu_temp:.0f} C"

            if gpu_temp is not None

            else "N/A"
        )

        power_text = (

            f"{gpu_power:.1f} W"

            if gpu_power is not None

            else "N/A"
        )

        clock_text = (

            f"{gpu_clock:.0f} MHz"

            if gpu_clock is not None

            else "N/A"
        )

        log_event(
            f"{self.log_prefix} | "
            f"RAM {ram_text} | "
            f"CPU {cpu_text}"
            f"{cpu_temp_text} | "
            f"GPU {gpu_text} | "
            f"VRAM {vram_text} | "
            f"GPU TEMP {temp_text} | "
            f"GPU POWER {power_text} | "
            f"GPU CLOCK {clock_text}"
        )


# ============================================================================
# COMFYUI MEMORY CLEANUP
# ============================================================================

def comfyui_free_memory(
    base_url
):

    """
    Tell ComfyUI to unload cached models
    and free memory.
    """

    url = (
        base_url.rstrip("/")
        + "/free"
    )

    payload = {
        "unload_models": True,
        "free_memory": True,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=60,
        )

        if not response.ok:

            log_event(
                "COMFYUI /free FAILED "
                f"HTTP {response.status_code}"
            )

            print(
                "[CLEANUP] ComfyUI /free "
                f"returned HTTP "
                f"{response.status_code}: "
                f"{response.text}"
            )

            return False

        log_event(
            "COMFYUI /free SUCCESS"
        )

        print(
            "[CLEANUP] ComfyUI released "
            "cached models/memory."
        )

        return True

    except requests.RequestException as exc:

        log_event(
            "COMFYUI /free REQUEST ERROR: "
            f"{exc}"
        )

        print(
            "[CLEANUP] Could not call "
            f"ComfyUI /free: {exc}"
        )

        return False


def python_cleanup():

    """
    Python-side cleanup.

    This does NOT unload models owned
    by ComfyUI.

    That is why /free is executed first.
    """

    collected = gc.collect()

    print(
        "[CLEANUP] Python GC released "
        f"{collected} objects."
    )

    try:

        import torch

        if torch.cuda.is_available():

            torch.cuda.empty_cache()

            torch.cuda.ipc_collect()

            print(
                "[CLEANUP] Local torch CUDA "
                "cache emptied."
            )

    except Exception as exc:

        print(
            "[CLEANUP] Local torch CUDA "
            f"cleanup not available: {exc}"
        )


# ============================================================================
# WAIT FOR VRAM
# ============================================================================

def wait_for_vram_below(
    limit_gb=VRAM_LIMIT_GB,
    timeout=RESOURCE_WAIT_TIMEOUT,
):

    print()

    print(
        "[WAIT VRAM] Waiting for VRAM "
        f"< {limit_gb:.2f} GB..."
    )

    log_event(
        f"WAITING FOR VRAM < "
        f"{limit_gb:.2f} GB"
    )

    started = time.time()

    last_report = 0

    while True:

        stats = get_system_stats()

        vram = (
            stats["vram_used_gb"]
        )

        now = time.time()

        if (
            vram is not None
            and vram < limit_gb
        ):

            log_event(
                "VRAM BELOW LIMIT | "
                f"{vram:.2f} GB"
            )

            print(
                "[WAIT VRAM] OK: "
                f"{vram:.2f} GB < "
                f"{limit_gb:.2f} GB"
            )

            return stats

        if (
            now - started
            > timeout
        ):

            raise TimeoutError(
                "VRAM did not fall below "
                f"{limit_gb} GB within "
                f"{timeout} seconds."
            )

        if (
            now - last_report
            >= 5
        ):

            if vram is None:

                log_event(
                    "WAIT VRAM | "
                    "VRAM = N/A"
                )

                print(
                    "[WAIT VRAM] "
                    "VRAM = N/A"
                )

            else:

                log_event(
                    "WAIT VRAM | "
                    f"VRAM {vram:.2f} GB"
                )

                print(
                    "\r[WAIT VRAM] "
                    f"{vram:.2f} GB / "
                    f"limit "
                    f"{limit_gb:.2f} GB",
                    end="",
                    flush=True,
                )

            last_report = now

        time.sleep(
            RESOURCE_CHECK_INTERVAL
        )


# ============================================================================
# WAIT FOR RAM
# ============================================================================

def wait_for_ram_below(
    limit_gb=RAM_LIMIT_GB,
    timeout=RESOURCE_WAIT_TIMEOUT,
):

    print()

    print(
        "[WAIT RAM] Waiting for RAM "
        f"< {limit_gb:.1f} GB..."
    )

    log_event(
        f"WAITING FOR RAM < "
        f"{limit_gb:.1f} GB"
    )

    started = time.time()

    last_report = 0

    while True:

        stats = get_system_stats()

        ram = (
            stats["ram_used_gb"]
        )

        now = time.time()

        if (
            ram is not None
            and ram < limit_gb
        ):

            log_event(
                "RAM BELOW LIMIT | "
                f"{ram:.1f} GB"
            )

            print(
                "[WAIT RAM] OK: "
                f"{ram:.1f} GB < "
                f"{limit_gb:.1f} GB"
            )

            return stats

        if (
            now - started
            > timeout
        ):

            raise TimeoutError(
                "RAM did not fall below "
                f"{limit_gb} GB within "
                f"{timeout} seconds."
            )

        if (
            now - last_report
            >= 5
        ):

            if ram is None:

                log_event(
                    "WAIT RAM | RAM = N/A"
                )

                print(
                    "[WAIT RAM] RAM = N/A"
                )

            else:

                log_event(
                    "WAIT RAM | "
                    f"RAM {ram:.1f} GB"
                )

                print(
                    "\r[WAIT RAM] "
                    f"{ram:.1f} GB / "
                    f"limit "
                    f"{limit_gb:.1f} GB",
                    end="",
                    flush=True,
                )

            last_report = now

        time.sleep(
            RESOURCE_CHECK_INTERVAL
        )


# ============================================================================
# ADDITIONAL COOLDOWN
# ============================================================================

def additional_cooldown(
    seconds=ADDITIONAL_COOLDOWN_SECONDS,
):

    print()

    print(
        "[COOLDOWN] Memory thresholds "
        "reached."
    )

    print(
        "[COOLDOWN] Waiting additional "
        f"{seconds} seconds..."
    )

    log_event(
        "ADDITIONAL COOLDOWN START | "
        f"{seconds}s"
    )

    for remaining in range(
        seconds,
        0,
        -1,
    ):

        if (
            remaining == seconds
            or remaining % 5 == 0
            or remaining <= 3
        ):

            stats = get_system_stats()

            ram = (
                stats["ram_used_gb"]
            )

            vram = (
                stats["vram_used_gb"]
            )

            temp = (
                stats["gpu_temp_c"]
            )

            ram_text = (

                f"{ram:.1f} GB"

                if ram is not None

                else "N/A"
            )

            vram_text = (

                f"{vram:.2f} GB"

                if vram is not None

                else "N/A"
            )

            temp_text = (

                f"{temp:.0f} C"

                if temp is not None

                else "N/A"
            )

            log_event(
                f"COOLDOWN {remaining}s | "
                f"RAM {ram_text} | "
                f"VRAM {vram_text} | "
                f"GPU {temp_text}"
            )

            print(
                f"\r[COOLDOWN] "
                f"{remaining:2d}s | "
                f"RAM {ram_text} | "
                f"VRAM {vram_text} | "
                f"GPU {temp_text}     ",
                end="",
                flush=True,
            )

        time.sleep(1)

    print()

    print(
        "[COOLDOWN] Additional "
        "cooldown complete."
    )

    log_event(
        "ADDITIONAL COOLDOWN COMPLETE"
    )


# ============================================================================
# WAIT FOR GPU TEMPERATURE
# ============================================================================

def wait_for_gpu_temperature(
    limit_c=GPU_TEMP_LIMIT_C,
    timeout=RESOURCE_WAIT_TIMEOUT,
):

    print()

    print(
        "[WAIT TEMP] Waiting for GPU "
        f"temperature < {limit_c:.0f} C..."
    )

    log_event(
        f"WAITING FOR GPU TEMP < "
        f"{limit_c:.0f} C"
    )

    started = time.time()

    last_report = 0

    while True:

        stats = get_system_stats()

        temp = (
            stats["gpu_temp_c"]
        )

        now = time.time()

        if (
            temp is not None
            and temp < limit_c
        ):

            log_event(
                "GPU TEMPERATURE SAFE | "
                f"{temp:.0f} C"
            )

            print(
                "[WAIT TEMP] OK: "
                f"{temp:.0f} C < "
                f"{limit_c:.0f} C"
            )

            return stats

        if (
            now - started
            > timeout
        ):

            raise TimeoutError(
                "GPU temperature did not "
                f"fall below {limit_c} C "
                f"within {timeout} seconds."
            )

        if (
            now - last_report
            >= 5
        ):

            if temp is None:

                log_event(
                    "WAIT TEMP | "
                    "GPU TEMP = N/A"
                )

                print(
                    "[WAIT TEMP] "
                    "GPU TEMP = N/A"
                )

            else:

                log_event(
                    "WAIT TEMP | "
                    f"GPU TEMP {temp:.0f} C"
                )

                print(
                    "\r[WAIT TEMP] "
                    f"GPU {temp:.0f} C / "
                    f"limit "
                    f"{limit_c:.0f} C",
                    end="",
                    flush=True,
                )

            last_report = now

        time.sleep(
            RESOURCE_CHECK_INTERVAL
        )


# ============================================================================
# FULL BETWEEN-LOOP CLEANUP
# ============================================================================

def cleanup_between_loops(
    base_url,
    pause_seconds=30,
):

    print()

    print("=" * 70)

    print(
        "POST-GENERATION CLEANUP / COOLING"
    )

    print("=" * 70)

    log_event(
        "POST-GENERATION CLEANUP START"
    )

    # ------------------------------------------------------------------------
    # 1. /free FIRST
    # ------------------------------------------------------------------------

    print(
        "[1/5] Requesting ComfyUI /free..."
    )

    log_event(
        "REQUESTING COMFYUI /free"
    )

    comfyui_free_memory(
        base_url
    )

    # ------------------------------------------------------------------------
    # 2. Python GC
    # ------------------------------------------------------------------------

    print(
        "[2/5] Running Python garbage "
        "collection..."
    )

    python_cleanup()

    print_system_stats(
        "AFTER /free + GC"
    )

    # ------------------------------------------------------------------------
    # 3. VRAM < 1 GB
    # ------------------------------------------------------------------------

    print(
        "[3/5] Waiting for VRAM "
        "threshold..."
    )

    wait_for_vram_below(
        VRAM_LIMIT_GB
    )

    # ------------------------------------------------------------------------
    # 4. RAM < 16 GB
    # ------------------------------------------------------------------------

    print(
        "[4/5] Waiting for RAM "
        "threshold..."
    )

    wait_for_ram_below(
        RAM_LIMIT_GB
    )

    # ------------------------------------------------------------------------
    # 5. Additional cooldown
    # ------------------------------------------------------------------------

    print(
        "[5/5] Memory thresholds "
        "reached."
    )

    additional_cooldown(
        pause_seconds
    )

    # ------------------------------------------------------------------------
    # Temperature check
    # ------------------------------------------------------------------------

    print()

    print(
        "[TEMPERATURE] Checking "
        "GPU temperature..."
    )

    stats = get_system_stats()

    if stats["gpu_temp_c"] is not None:

        log_event(
            "GPU TEMP AFTER COOLDOWN | "
            f"{stats['gpu_temp_c']:.0f} C"
        )

    # ------------------------------------------------------------------------
    # Wait until GPU < 70 C
    # ------------------------------------------------------------------------

    wait_for_gpu_temperature(
        GPU_TEMP_LIMIT_C
    )

    # ------------------------------------------------------------------------
    # Final verification
    # ------------------------------------------------------------------------

    print()

    final_stats = print_system_stats(
        "READY FOR NEXT LOOP"
    )

    log_event(
        "POST-GENERATION CLEANUP COMPLETE"
    )

    print("=" * 70)

    return final_stats


# ============================================================================
# MAIN
# ============================================================================

def main():

    logger = setup_logging()

    try:

        return _main_impl()

    except KeyboardInterrupt:

        log_event(
            "INTERRUPTED BY USER"
        )

        raise

    except Exception as exc:

        log_event(
            f"FATAL ERROR: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        import traceback

        traceback.print_exc()

        raise

    finally:

        logger.close()


def _main_impl():

    parser = argparse.ArgumentParser(
        description=(
            "Loop MiniMax H3 videos using "
            "each previous video final frame "
            "as the next first frame."
        )
    )

    parser.add_argument(
        "--workflow",
        required=True,
    )

    parser.add_argument(
        "--prompt",
        required=True,
    )

    parser.add_argument(
        "--image",
        required=True,
        help="Path to initial image for first frame.",
    )

    parser.add_argument(
        "--last-image",
        default=None,
        help=(
            "Optional path to ending target image (last frame) "
            "for the starting loop."
        ),
    )

    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
    )

    parser.add_argument(
        "--loops",
        type=int,
        default=0,
        help=(
            "Number of loops to execute. "
            "0 = all prompts from "
            "--start-loop."
        ),
    )

    parser.add_argument(
        "--start-loop",
        type=int,
        default=1,
        help=(
            "1-based prompt/loop number "
            "to start from. The supplied "
            "--image is used as its first frame."
        ),
    )

    # ------------------------------------------------------------------------
    # VIDEO RESOLUTION
    # ------------------------------------------------------------------------

    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help=(
            "Video width in pixels. "
            "If omitted, uses workflow value."
        ),
    )

    parser.add_argument(
        "--height",
        type=int,
        default=None,
        help=(
            "Video height in pixels. "
            "If omitted, uses workflow value."
        ),
    )

    # ------------------------------------------------------------------------
    # VIDEO DURATION
    # ------------------------------------------------------------------------

    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help=(
            "Video duration in seconds. "
            "If omitted, uses workflow value."
        ),
    )

    parser.add_argument(
        "--outdir",
        default="h3_loop_output",
    )

    parser.add_argument(
        "--pause",
        type=int,
        default=30,
        help=(
            "Additional cooldown seconds "
            "after RAM/VRAM thresholds "
            "are reached. Default: 30."
        ),
    )

    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "Kept for backwards compatibility. "
            "Cleanup is always performed "
            "between loops."
        ),
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Check the workflow and exit "
            "without generating."
        ),
    )

    args = parser.parse_args()

    # =========================================================================
    # ARGUMENT VALIDATION
    # =========================================================================

    if args.width is not None:

        if args.width <= 0:

            raise RuntimeError(
                "--width must be greater than 0."
            )

        if args.width % 32 != 0:

            raise RuntimeError(
                "--width must be divisible by 32."
            )

    if args.height is not None:

        if args.height <= 0:

            raise RuntimeError(
                "--height must be greater than 0."
            )

        if args.height % 32 != 0:

            raise RuntimeError(
                "--height must be divisible by 32."
            )

    if args.duration is not None:

        if args.duration <= 0:

            raise RuntimeError(
                "--duration must be greater than 0."
            )

    if args.loops < 0:

        raise RuntimeError(
            "--loops cannot be negative."
        )

    if args.start_loop < 1:

        raise RuntimeError(
            "--start-loop must be >= 1."
        )

    if args.pause < 0:

        raise RuntimeError(
            "--pause cannot be negative."
        )

    # =========================================================================
    # LOAD WORKFLOW
    # =========================================================================

    workflow_path = Path(
        args.workflow
    )

    prompt_path = Path(
        args.prompt
    )

    initial_image = Path(
        args.image
    )

    workflow = load_api_workflow(
        workflow_path
    )

    if args.check:

        print_workflow_check(
            workflow
        )

        return

    # =========================================================================
    # LOAD PROMPTS
    # =========================================================================

    all_prompts = split_prompts(
        prompt_path.read_text(
            encoding="utf-8"
        )
    )

    if (
        args.start_loop < 1
        or
        args.start_loop > len(all_prompts)
    ):

        raise RuntimeError(
            f"--start-loop must be between "
            f"1 and {len(all_prompts)}."
        )

    end_loop = len(
        all_prompts
    )

    if args.loops > 0:

        end_loop = min(
            args.start_loop
            + args.loops
            - 1,
            len(all_prompts),
        )

    prompts = all_prompts[
        args.start_loop - 1:
        end_loop
    ]

    if not initial_image.exists():

        raise FileNotFoundError(
            initial_image
        )

    if args.last_image:

        last_image_path = Path(
            args.last_image
        )

        if not last_image_path.exists():

            raise FileNotFoundError(
                last_image_path
            )

    # =========================================================================
    # OUTPUT
    # =========================================================================

    outdir = Path(
        args.outdir
    )

    outdir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # =========================================================================
    # INITIAL INFORMATION
    # =========================================================================

    print_workflow_check(
        workflow
    )

    print()

    workflow_width, workflow_height, workflow_duration = (
        get_workflow_video_parameters(
            workflow
        )
    )

    effective_width = (
        args.width
        if args.width is not None
        else workflow_width
    )

    effective_height = (
        args.height
        if args.height is not None
        else workflow_height
    )

    effective_duration = (
        args.duration
        if args.duration is not None
        else workflow_duration
    )

    print(
        f"Prompts available: "
        f"{len(all_prompts)}"
    )

    print(
        f"Starting at loop: "
        f"{args.start_loop}"
    )

    print(
        f"Executing loops: "
        f"{args.start_loop}-"
        f"{end_loop}"
    )

    print(
        f"Initial image for starting loop: "
        f"{initial_image}"
    )

    print()

    print(
        "VIDEO CONFIGURATION"
    )

    print(
        f"  Width:     {effective_width}"
    )

    print(
        f"  Height:    {effective_height}"
    )

    print(
        f"  Duration:  {effective_duration}s"
    )

    print()

    if args.width is None:

        print(
            "Width: using workflow default."
        )

    else:

        print(
            "Width: overridden from CLI."
        )

    if args.height is None:

        print(
            "Height: using workflow default."
        )

    else:

        print(
            "Height: overridden from CLI."
        )

    if args.duration is None:

        print(
            "Duration: using workflow default."
        )

    else:

        print(
            "Duration: overridden from CLI."
        )

    print()

    print_system_stats(
        "BEFORE START"
    )

    log_event(
        "RUN INITIALIZED"
    )

    print()

    current_image = (
        initial_image
    )

    # =========================================================================
    # LOOP
    # =========================================================================

    for offset, prompt in enumerate(
        prompts
    ):

        index = (
            args.start_loop
            + offset
        )

        print("=" * 70)

        print(
            f"LOOP {index}/"
            f"{len(all_prompts)}"
        )

        log_event(
            f"LOOP {index} START"
        )

        log_event(
            "PROMPT LENGTH: "
            f"{len(prompt)} characters"
        )

        print("=" * 70)

        print(
            prompt
        )

        print()

        # ---------------------------------------------------------------------
        # Upload first frame
        # ---------------------------------------------------------------------

        upload_name = (
            f"h3_loop_"
            f"{index:04d}_first.png"
        )

        print(
            "Uploading first frame "
            "to ComfyUI..."
        )

        (
            uploaded_name,
            subfolder,
            image_type,
        ) = upload_image(
            args.base_url,
            current_image,
            filename=upload_name,
        )

        if subfolder:

            comfy_image_name = (
                f"{subfolder}/"
                f"{uploaded_name}"
            )

        else:

            comfy_image_name = (
                uploaded_name
            )

        # ---------------------------------------------------------------------
        # Upload last frame (optional target image for starting loop)
        # ---------------------------------------------------------------------

        comfy_last_image_name = None

        if index == args.start_loop and args.last_image:

            last_upload_name = (
                f"h3_loop_"
                f"{index:04d}_last_target.png"
            )

            print(
                "Uploading last target frame "
                "to ComfyUI..."
            )

            (
                uploaded_l_name,
                l_subfolder,
                _,
            ) = upload_image(
                args.base_url,
                Path(args.last_image),
                filename=last_upload_name,
            )

            if l_subfolder:

                comfy_last_image_name = (
                    f"{l_subfolder}/"
                    f"{uploaded_l_name}"
                )

            else:

                comfy_last_image_name = (
                    uploaded_l_name
                )

        # ---------------------------------------------------------------------
        # Modify workflow
        # ---------------------------------------------------------------------

        wf = modify_workflow(
            workflow,
            prompt,
            comfy_image_name,
            index,
            width=args.width,
            height=args.height,
            duration=args.duration,
            uploaded_last_filename=comfy_last_image_name,
        )

        actual_width, actual_height, actual_duration = (
            get_workflow_video_parameters(
                wf
            )
        )

        print(
            f"Node {IMAGE_NODE} image: "
            f"{comfy_image_name}"
        )

        print(
            "Final-frame capture: "
            "PreviewImage 56 <- "
            "VAEDecode 35"
        )

        print(
            "Video resolution: "
            f"{actual_width}x"
            f"{actual_height}"
        )

        print(
            "Video duration: "
            f"{actual_duration}s"
        )

        h3_inputs = wf[PROMPT_NODE]["inputs"]
        has_last_frame = "last_frame" in h3_inputs

        if comfy_last_image_name:
            print(
                "Temporal mode: EXPLICIT "
                "last-frame target enabled "
                "(--last-frame)."
            )
        else:
            print(
                "Temporal mode: FIRST-FRAME ONLY "
                "| no last-frame target."
            )

        # Print the exact payload state that is about to be submitted.
        print(
            f"PAYLOAD VALIDATION | "
            f"node {PROMPT_NODE}.first_frame="
            f"{h3_inputs.get('first_frame')}"
        )
        print(
            f"PAYLOAD VALIDATION | "
            f"node {PROMPT_NODE}.last_frame="
            f"{h3_inputs.get('last_frame')}"
        )
        print(
            f"PAYLOAD VALIDATION | "
            f"node {LAST_FRAME_NODE} exists="
            f"{LAST_FRAME_NODE in wf}"
        )
        print(
            f"PAYLOAD VALIDATION | "
            f"node {LAST_FRAME_IMAGE_NODE} exists="
            f"{LAST_FRAME_IMAGE_NODE in wf}"
        )

        # Final fail-safe: never submit an unauthorized target.
        if not comfy_last_image_name and (
            has_last_frame or
            LAST_FRAME_NODE in wf or
            LAST_FRAME_IMAGE_NODE in wf
        ):
            raise RuntimeError(
                "REFUSING TO SUBMIT: unauthorized last-frame target "
                "detected in the effective ComfyUI payload."
            )

        print(
            "Submitting workflow "
            "to ComfyUI..."
        )

        # ---------------------------------------------------------------------
        # IMPORTANT:
        # Start timer BEFORE queueing.
        # ---------------------------------------------------------------------

        started = time.time()

        log_event(
            f"LOOP {index} "
            "SUBMITTED TO COMFYUI"
        )

        prompt_id = queue_prompt(
            args.base_url,
            wf,
        )

        print(
            f"Prompt ID: {prompt_id}"
        )

        print(
            "Waiting for ComfyUI..."
        )

        # ---------------------------------------------------------------------
        # Generation monitor
        # ---------------------------------------------------------------------

        monitor = GenerationMonitor(
            interval=MONITOR_INTERVAL_SECONDS,
            log_prefix=(
                f"LOOP {index} MONITOR"
            ),
        )

        monitor.start()

        try:

            history = wait_for_history(
                args.base_url,
                prompt_id,
            )

        finally:

            monitor.stop()

        # ---------------------------------------------------------------------
        # VIDEO COMPLETED
        # ---------------------------------------------------------------------

        print(
            "ComfyUI job completed."
        )

        log_event(
            f"LOOP {index} "
            "VIDEO COMPLETED"
        )

        # ---------------------------------------------------------------------
        # LAST FRAME:
        # SAVE IMMEDIATELY BEFORE CLEANUP
        # ---------------------------------------------------------------------

        images = find_preview_images(
            history
        )

        last = images[-1]

        last_frame_path = (
            outdir
            / f"loop_{index:04d}_last.png"
        )

        # IMPORTANT:
        # This happens BEFORE /free.

        download_comfy_image(
            args.base_url,
            last,
            last_frame_path,
        )

        generation_seconds = (
            time.time()
            - started
        )

        log_event(
            "VIDEO GENERATED IN "
            f"{generation_seconds:.1f} "
            "SECONDS"
        )

        print(
            "Last frame saved immediately: "
            f"{last_frame_path}"
        )

        log_event(
            "LAST FRAME SAVED: "
            f"{last_frame_path}"
        )

        # ---------------------------------------------------------------------
        # SAVE AUDIO + VIDEO BEFORE CLEANUP
        # ---------------------------------------------------------------------
        audio_outputs = find_audio_outputs(history)
        audio_info = audio_outputs[-1]
        audio_path = outdir / f"loop_{index:04d}_audio.flac"
        download_comfy_media(
            args.base_url,
            audio_info,
            audio_path,
        )
        log_event(f"AUDIO SAVED: {audio_path}")

        # Save the actual ComfyUI video too. This lets us create one final
        # master video after all loops while preserving the generated frames.
        video_outputs = []
        save_video_output = history.get("outputs", {}).get(str(SAVE_VIDEO_NODE), {})
        for key in ("gifs", "videos", "video"):
            video_outputs.extend(_normalize_media_entries(save_video_output.get(key)))
        if video_outputs:
            video_info = video_outputs[-1]
            ext = Path(video_info["filename"]).suffix or ".mp4"
            video_path = outdir / f"loop_{index:04d}{ext}"
            download_comfy_media(args.base_url, video_info, video_path)
            log_event(f"VIDEO SAVED: {video_path}")
        else:
            log_event("WARNING: SaveVideo output metadata was not found; final video assembly may be unavailable.")

        # ---------------------------------------------------------------------
        # This becomes first frame of next loop.
        # ---------------------------------------------------------------------

        current_image = (
            last_frame_path
        )

        log_event(
            f"LOOP {index} COMPLETE"
        )

        print_system_stats(
            f"AFTER LOOP {index}"
        )

        # ---------------------------------------------------------------------
        # CLEANUP BETWEEN LOOPS
        # Never needed after final loop.
        # ---------------------------------------------------------------------

        if index < end_loop:

            cleanup_between_loops(
                args.base_url,
                args.pause,
            )

        else:

            print()

            print(
                "[FINAL LOOP] No cleanup/"
                "cooldown required after "
                "final generation."
            )

            log_event(
                "FINAL LOOP COMPLETED"
            )

        print()

    # =========================================================================
    # FINAL AUDIO / VIDEO ASSEMBLY
    # =========================================================================
    print()
    print("=" * 70)
    print("BUILDING MASTER AUDIO / VIDEO")
    print("=" * 70)

    master_audio = build_master_audio(outdir, end_loop)
    log_event(f"MASTER AUDIO CREATED: {master_audio}")
    print(f"Master audio: {master_audio.resolve()}")

    try:
        master_video = build_master_video(outdir, end_loop, master_audio)
        log_event(f"MASTER VIDEO CREATED: {master_video}")
        print(f"Master video: {master_video.resolve()}")
    except Exception as exc:
        log_event(f"MASTER VIDEO ASSEMBLY WARNING: {exc}")
        print(f"WARNING: master video could not be assembled: {exc}")

    # =========================================================================
    # FINISHED
    # =========================================================================

    print("=" * 70)

    print(
        "ALL LOOPS COMPLETED"
    )

    print("=" * 70)

    print(
        "Output directory: "
        f"{outdir.resolve()}"
    )

    log_event(
        "ALL LOOPS COMPLETED "
        "SUCCESSFULLY"
    )


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":

    main()

