import base64
import os

from dotenv import load_dotenv
from openai import OpenAI

# 从 .env 文件中加载环境变量
load_dotenv()
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY") or "替换为你的API_KEY",
    base_url=os.getenv("OPENAI_BASE_URL") or None,
)

stream = client.responses.create(
    model=os.getenv("TEXT_MODEL") or "gpt-5.5",
    input="Draw a gorgeous image of a river made of white owl feathers, snaking its way through a serene winter landscape",
    stream=True,
    tools=[{"type": "image_generation", "partial_images": 3}],
)

for event in stream:
    if event.type == "response.image_generation_call.partial_image":
        idx = event.partial_image_index
        image_base64 = event.partial_image_b64
        image_bytes = base64.b64decode(image_base64)
        with open(f"river{idx}.png", "wb") as f:
            f.write(image_bytes)
