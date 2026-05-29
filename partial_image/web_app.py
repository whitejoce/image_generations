import base64
import json
import mimetypes
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Iterable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
INDEX_HTML = BASE_DIR / "index.html"

load_dotenv(BASE_DIR / ".env")

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2")
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-5.5")

SIZE_OPTIONS = {
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
}
QUALITY_OPTIONS = {"auto", "low", "medium", "high"}
PARTIAL_IMAGE_OPTIONS = {0, 1, 2, 3}
FORMAT_OPTIONS = {"png", "jpeg", "webp"}
BACKGROUND_OPTIONS = {"auto", "opaque"}
MODERATION_OPTIONS = {"auto", "low"}
INPUT_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_INPUT_IMAGES = 16
MAX_INPUT_IMAGE_BYTES = 50 * 1024 * 1024


app = FastAPI(title="partial_image stream debugger")


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str | None = None
    size: str = "auto"
    quality: str = "auto"
    partial_images: int = 2
    image_count: int = Field(1, ge=1, le=10)
    output_format: str = "png"
    output_compression: int = Field(100, ge=0, le=100)
    background: str = "auto"
    moderation: str = "auto"


class OptimizePromptRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str | None = None


@dataclass(frozen=True)
class InputImage:
    filename: str
    content_type: str
    content: bytes


def create_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("api-key")
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("base-url") or None
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Missing API key. Set OPENAI_API_KEY or api-key in partial_image/.env.",
        )
    return OpenAI(api_key=api_key, base_url=base_url)


def to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(warnings=False)
    if isinstance(value, dict):
        return value
    return value


def event_type(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("type", ""))
    return str(getattr(event, "type", ""))


def get_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def iter_values(value: Any) -> Iterable[Any]:
    plain = to_plain(value)
    if isinstance(plain, dict):
        yield plain
        for item in plain.values():
            yield from iter_values(item)
    elif isinstance(plain, list):
        for item in plain:
            yield from iter_values(item)


def ndjson(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def validate_size(size: str) -> None:
    if size == "auto":
        return
    if "x" not in size:
        raise HTTPException(
            status_code=400, detail="Size must be auto or WIDTHxHEIGHT."
        )
    try:
        width_text, height_text = size.lower().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Size must be auto or WIDTHxHEIGHT."
        ) from exc

    total_pixels = width * height
    long_edge = max(width, height)
    short_edge = min(width, height)
    if long_edge > 3840:
        raise HTTPException(
            status_code=400, detail="Maximum edge length must be <= 3840."
        )
    if width % 16 != 0 or height % 16 != 0:
        raise HTTPException(
            status_code=400, detail="Both size edges must be multiples of 16."
        )
    if long_edge / short_edge > 3:
        raise HTTPException(
            status_code=400, detail="Long edge to short edge ratio must be <= 3:1."
        )
    if total_pixels < 655_360 or total_pixels > 8_294_400:
        raise HTTPException(
            status_code=400,
            detail="Total pixels must be between 655,360 and 8,294,400.",
        )


def validate_generate_request(request: GenerateRequest) -> None:
    validate_size(request.size)
    if request.quality not in QUALITY_OPTIONS:
        raise HTTPException(status_code=400, detail="Unsupported quality option.")
    if request.partial_images not in PARTIAL_IMAGE_OPTIONS:
        raise HTTPException(
            status_code=400, detail="partial_images must be 0, 1, 2, or 3."
        )
    if request.partial_images > 0 and request.image_count != 1:
        raise HTTPException(
            status_code=400,
            detail="image_count must be 1 when partial_images is greater than 0.",
        )
    if request.output_format not in FORMAT_OPTIONS:
        raise HTTPException(status_code=400, detail="Unsupported output_format option.")
    if request.background not in BACKGROUND_OPTIONS:
        raise HTTPException(status_code=400, detail="Unsupported background option.")
    if request.moderation not in MODERATION_OPTIONS:
        raise HTTPException(status_code=400, detail="Unsupported moderation option.")


def validate_input_images(input_images: list[InputImage]) -> None:
    if len(input_images) > MAX_INPUT_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Upload at most {MAX_INPUT_IMAGES} reference images.",
        )
    for image in input_images:
        if not image.content:
            raise HTTPException(status_code=400, detail=f"{image.filename} is empty.")
        if len(image.content) > MAX_INPUT_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"{image.filename} must be smaller than 50MB.",
            )
        if image.content_type not in INPUT_IMAGE_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"{image.filename} must be PNG, JPEG, or WebP.",
            )


def safe_filename(filename: str, index: int, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        suffix = mimetypes.guess_extension(content_type) or ".png"
        if suffix == ".jpe":
            suffix = ".jpg"
    stem = Path(filename).stem or f"image_{index}"
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)
    return f"{index:02d}_{cleaned[:64]}{suffix}"


def write_image(run_dir: Path, name: str, image_base64: str) -> int:
    image_bytes = base64.b64decode(image_base64)
    path = run_dir / name
    path.write_bytes(image_bytes)
    return len(image_bytes)


def write_input_images(
    run_dir: Path, input_images: list[InputImage]
) -> list[dict[str, Any]]:
    if not input_images:
        return []
    inputs_dir = run_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for index, image in enumerate(input_images):
        filename = safe_filename(image.filename, index, image.content_type)
        (inputs_dir / filename).write_bytes(image.content)
        saved.append(
            {
                "filename": filename,
                "original_filename": image.filename,
                "content_type": image.content_type,
                "bytes": len(image.content),
            }
        )
    return saved


def write_run_metadata(
    run_dir: Path,
    request: GenerateRequest,
    model: str,
    mode: str,
    input_metadata: list[dict[str, Any]],
) -> None:
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "model": model,
        "prompt": request.prompt,
        "parameters": {
            "size": request.size,
            "quality": request.quality,
            "partial_images": request.partial_images,
            "image_count": request.image_count,
            "output_format": request.output_format,
            "output_compression": request.output_compression,
            "background": request.background,
            "moderation": request.moderation,
        },
        "input_images": input_metadata,
    }
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def image_generate_kwargs(request: GenerateRequest, model: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "prompt": request.prompt,
        "n": request.image_count,
        "size": request.size,
        "quality": request.quality,
        "output_format": request.output_format,
        "background": request.background,
        "moderation": request.moderation,
    }
    if request.output_format in {"jpeg", "webp"}:
        kwargs["output_compression"] = request.output_compression
    return kwargs


def image_edit_kwargs(
    request: GenerateRequest, model: str, input_images: list[InputImage]
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "prompt": request.prompt,
        "image": [
            (image.filename, image.content, image.content_type)
            for image in input_images
        ],
        "n": request.image_count,
        "size": request.size,
        "quality": request.quality,
        "output_format": request.output_format,
        "background": request.background,
    }
    if request.output_format in {"jpeg", "webp"}:
        kwargs["output_compression"] = request.output_compression
    return kwargs


def response_images(response: Any) -> list[str]:
    data = get_field(response, "data", []) or []
    images: list[str] = []
    for item in data:
        image_base64 = get_field(item, "b64_json")
        if image_base64:
            images.append(image_base64)
    return images


def generation_stream(
    request: GenerateRequest, input_images: list[InputImage] | None = None
) -> Generator[str, None, None]:
    input_images = input_images or []
    client = create_client()
    model = request.model or IMAGE_MODEL
    mode = "edit" if input_images else "generate"
    started = time.perf_counter()
    run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    input_metadata = write_input_images(run_dir, input_images)
    write_run_metadata(run_dir, request, model, mode, input_metadata)

    yield ndjson(
        {
            "type": "log",
            "message": f"Starting Image API {mode} with {model}.",
            "run_dir": str(run_dir),
        }
    )
    if input_metadata:
        yield ndjson(
            {
                "type": "log",
                "message": f"mode=edit input_images={len(input_metadata)}",
                "inputs": input_metadata,
            }
        )

    try:
        final_count = 0
        kwargs = (
            image_edit_kwargs(request, model, input_images)
            if input_images
            else image_generate_kwargs(request, model)
        )
        create_image = client.images.edit if input_images else client.images.generate
        partial_event = (
            "image_edit.partial_image"
            if input_images
            else "image_generation.partial_image"
        )
        completed_event = (
            "image_edit.completed" if input_images else "image_generation.completed"
        )

        if request.partial_images == 0:
            response = create_image(**kwargs)
            for image_base64 in response_images(response):
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                byte_count = write_image(
                    run_dir,
                    f"final_{final_count}.{request.output_format}",
                    image_base64,
                )
                yield ndjson(
                    {
                        "type": "final_image",
                        "index": final_count,
                        "b64": image_base64,
                        "elapsed_ms": elapsed_ms,
                        "bytes": byte_count,
                        "format": request.output_format,
                    }
                )
                final_count += 1
        else:
            kwargs["stream"] = True
            kwargs["partial_images"] = request.partial_images
            stream = create_image(**kwargs)
            for event in stream:
                kind = event_type(event)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                if kind == partial_event:
                    index = get_field(event, "partial_image_index", 0)
                    image_base64 = get_field(event, "b64_json")
                    if not image_base64:
                        yield ndjson(
                            {
                                "type": "log",
                                "message": "Partial image event had no image data.",
                            }
                        )
                        continue
                    byte_count = write_image(
                        run_dir,
                        f"partial_{index}.{request.output_format}",
                        image_base64,
                    )
                    yield ndjson(
                        {
                            "type": "partial_image",
                            "index": index,
                            "b64": image_base64,
                            "elapsed_ms": elapsed_ms,
                            "bytes": byte_count,
                            "format": request.output_format,
                        }
                    )
                elif kind == completed_event:
                    image_base64 = get_field(event, "b64_json")
                    if not image_base64:
                        yield ndjson(
                            {
                                "type": "log",
                                "message": "Completed event had no image data.",
                            }
                        )
                        continue
                    byte_count = write_image(
                        run_dir,
                        f"final_{final_count}.{request.output_format}",
                        image_base64,
                    )
                    yield ndjson(
                        {
                            "type": "final_image",
                            "index": final_count,
                            "b64": image_base64,
                            "elapsed_ms": elapsed_ms,
                            "bytes": byte_count,
                            "format": request.output_format,
                        }
                    )
                    final_count += 1
        yield ndjson(
            {
                "type": "done",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "run_dir": str(run_dir),
            }
        )
    except Exception as exc:  # noqa: BLE001 - expose API/debug errors to the local UI.
        yield ndjson({"type": "error", "message": str(exc), "run_dir": str(run_dir)})


def replay_stream() -> Generator[str, None, None]:
    started = time.perf_counter()
    files = sorted(BASE_DIR.glob("river*.png"))
    if not files:
        yield ndjson(
            {"type": "error", "message": "No river*.png files found in partial_image."}
        )
        return
    for index, path in enumerate(files):
        image_bytes = path.read_bytes()
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        yield ndjson(
            {
                "type": "partial_image" if index < len(files) - 1 else "final_image",
                "index": index,
                "b64": image_base64,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "bytes": len(image_bytes),
                "format": "png",
                "source": path.name,
            }
        )
        time.sleep(0.7)
    yield ndjson(
        {
            "type": "done",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "run_dir": str(BASE_DIR),
        }
    )


async def parse_generate_request(
    http_request: Request,
) -> tuple[GenerateRequest, list[InputImage]]:
    content_type = http_request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        try:
            form = await http_request.form()
        except AssertionError as exc:
            raise HTTPException(
                status_code=400,
                detail="Multipart form parsing requires python-multipart.",
            ) from exc

        fields = [
            "prompt",
            "model",
            "size",
            "quality",
            "partial_images",
            "image_count",
            "output_format",
            "output_compression",
            "background",
            "moderation",
        ]
        data = {}
        for field in fields:
            value = form.get(field)
            if value not in {None, ""}:
                data[field] = value

        input_images: list[InputImage] = []
        for key in ("images", "images[]"):
            for upload in form.getlist(key):
                filename = getattr(upload, "filename", "")
                if not filename:
                    continue
                content = await upload.read()
                content_type = (
                    getattr(upload, "content_type", None)
                    or mimetypes.guess_type(filename)[0]
                    or ""
                )
                input_images.append(
                    InputImage(
                        filename=filename,
                        content_type=content_type,
                        content=content,
                    )
                )
        try:
            generate_request = GenerateRequest(**data)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return generate_request, input_images

    try:
        payload = await http_request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail="Invalid JSON request body."
        ) from exc
    try:
        generate_request = GenerateRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return generate_request, []


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@app.post("/api/generate")
async def generate(http_request: Request) -> StreamingResponse:
    request, input_images = await parse_generate_request(http_request)
    validate_generate_request(request)
    validate_input_images(input_images)
    create_client()
    return StreamingResponse(
        generation_stream(request, input_images), media_type="application/x-ndjson"
    )


@app.post("/api/replay")
def replay() -> StreamingResponse:
    return StreamingResponse(replay_stream(), media_type="application/x-ndjson")


@app.post("/api/optimize-prompt")
def optimize_prompt(request: OptimizePromptRequest) -> dict[str, str]:
    client = create_client()
    model = request.model or TEXT_MODEL
    instruction = (
        "Rewrite the user's image generation prompt for gpt-image-2. "
        "Preserve the original intent. Add concrete visual detail for subject, style, "
        "composition, lighting, materials, camera perspective, background, and mood. "
        "Do not add conflicting objects, brands, identities, or text. "
        "Return only the rewritten prompt with no explanation."
    )
    try:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": request.prompt},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - local debugging endpoint.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    output_text = getattr(response, "output_text", None)
    if not output_text:
        plain = to_plain(response)
        output_text = ""
        for value in iter_values(plain):
            if isinstance(value, dict) and value.get("type") in {"output_text", "text"}:
                text = value.get("text")
                if isinstance(text, str):
                    output_text += text
    return {"prompt": (output_text or "").strip(), "model": model}
