# Partial Image
<div align="center">
  <a href="./README_CN.md">中文</a>
</div>

> Coded with GPT-5.5 for the GPT-image-2 partial image API demo and testing.

A local web tool for debugging and demonstrating streamed intermediate images from the OpenAI image generation API. The project supports text-to-image generation, image editing with reference images, real-time partial image previews, prompt optimization, and local saving of output images and run metadata.

![Demo](images/demo.png)

## Features

- Preview partial images in real time during image generation
- Generate images from text prompts
- Edit images with uploaded reference images
- Export PNG, JPEG, or WebP images
- Configure size, quality, compression, background, and moderation options
- Optimize image generation prompts with one click
- Save generated images, uploaded inputs, and `metadata.json` for each run
- Replay the bundled `river*.png` sample images without calling the API

## Project Structure

```text
partial_image/
├── index.html          # Frontend page
├── web_app.py          # FastAPI backend service
├── partial_image.py    # Minimal partial image API example script
├── river0.png          # Sample partial image
├── river1.png          # Sample partial image
├── river2.png          # Sample final image
├── runs/               # Local output directory, do not commit to GitHub
└── .env                # Local environment variables, do not commit to GitHub
```

## Requirements

- Python 3.10 or later
- A valid OpenAI API key

Install dependencies:

```bash
pip install -r requirements.txt
```

Or install them manually:

```bash
pip install openai python-dotenv fastapi uvicorn python-multipart
```

## Environment Variables

Configure your API key in `partial_image/.env`:

```env
api-key=YOUR_API_KEY
base-url=https://api.openai.com/v1
```

Notes:

- `OPENAI_API_KEY`: required OpenAI API key
- `OPENAI_BASE_URL`: optional custom API base URL
- `IMAGE_MODEL`: editable in the web page, defaults to `gpt-image-2`
- `TEXT_MODEL`: defaults to `gpt-5.5`, used for prompt optimization

The backend also supports the legacy `.env` names `api-key` and `base-url`.

## Run The Web App

Run this from the project root:

```bash
cd partial_image
uvicorn web_app:app --reload --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

## Usage

### Generate Images

1. Enter an image prompt in the web page.
2. Choose size, quality, output format, and other parameters.
3. Set `partial_images` to `1`, `2`, or `3` to preview intermediate images while generation is running.
4. Start generation. The page will show partial images first, then the final image.

Generated results are saved to:

```text
partial_image/runs/<timestamp>/
```

Each run directory usually contains:

- `partial_*.png`: intermediate images
- `final_*.png`: final images
- `metadata.json`: prompt, parameters, and input image metadata
- `inputs/`: uploaded reference images

### Edit With Reference Images

Upload PNG, JPEG, or WebP images in the page before generating. The backend will call the image editing API instead of plain text-to-image generation.

Limits:

- Up to 16 reference images
- Maximum 50 MB per image
- PNG, JPEG, and WebP are supported

### Replay Sample Images

The project includes `river0.png`, `river1.png`, and `river2.png`. You can use the replay feature in the web page to preview the partial image display flow without making an API request.

### Minimal Script Example

You can also run `partial_image.py` directly:

```bash
cd partial_image
python partial_image.py
```

The script requests one image generation and saves streamed partial images as `river0.png`, `river1.png`, and `river2.png`.

## API Endpoints

### `POST /api/generate`

Generate or edit images. The response is an NDJSON stream.

Example JSON request:

```json
{
  "prompt": "A river made of white owl feathers winding through a quiet winter forest",
  "size": "1024x1024",
  "quality": "auto",
  "partial_images": 2,
  "image_count": 1,
  "output_format": "png",
  "background": "auto",
  "moderation": "auto"
}
```

### `POST /api/optimize-prompt`

Optimize an image generation prompt.

```json
{
  "prompt": "A feather river in the snow"
}
```

### `POST /api/replay`

Replay the bundled `river*.png` sample images from the project directory.
