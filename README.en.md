<p align="center">
  <img src="assets/branding/glimpsely-bunny-black-white.png" width="160" alt="Glimpsely bunny logo">
</p>

<h1 align="center">Glimpsely</h1>
<p align="center"><strong>Send it over. Let the little things be remembered.</strong></p>
<p align="center">Your personal memory assistant in WeChat · Screenshots · Recall · Everyday suggestions</p>

| [简体中文](README.md) | **English · Selected** |
| :---: | :---: |

A screenshot saved somewhere. A coupon forgotten until it expires. A pickup code buried in a conversation.

**Glimpsely turns a WeChat chat into a personal memory assistant.** Send screenshots, text or voice messages; it organizes them into searchable memories. Ask a question to find information again, or request a brief with suggestions drawn from your recent records.

## How can it help?

| What you send | What it helps with |
| --- | --- |
| Delivery notices and receipts | Remember pickup codes, tracking numbers and other details |
| Coupons, bills and event screenshots | Extract deadlines and attempt a reminder within the final hour for active records |
| Places, conversation screenshots and everyday ideas | Save the content and image text for later questions |
| A few days of collected memories | Create a short brief with concrete suggestions and inspiration |

Talk naturally—no command vocabulary to memorize. Example interaction, with replies depending on the actual image and model:

> **You:** [Send a delivery screenshot]<br>
> **Glimpsely:** Saved ✓ SF Express parcel, pickup code 8-2-3002.<br>
> **You:** What was that pickup code?<br>
> **Glimpsely:** 8-2-3002.

Try “remember my meeting at eight tonight” or ask for a brief. Voice input requires a local speech model. **Current prompts and responses are primarily Chinese**; the exchange above is translated for illustration.

## Made for everyday recall

- **Understand before saving.** OCR extracts screenshot text, then a multimodal model interprets it. Multiple images in one message are supported.
- **Ask and retrieve.** Answers use relevant stored records and the text captured from images.
- **Go beyond collecting.** Briefs turn recent records into suggestions about tasks, coupons and everyday habits. They use saved content, not live web recommendations.
- **Local storage and inference by default.** Memories live in SQLite and models run locally. Messages still travel through WeChat; configuring a remote model sends relevant content to that service.

## Get started

This is a **self-hosted, single-user** project. You need a computer that stays running and a WeChat account for the bot to chat with you. Existing launch scripts target macOS with omlx; Python 3.12 is recommended.

### 1. Install and configure

After downloading the project, run from its root directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install 'wechat-ilink-bot>=0.1.0' 'apscheduler>=3.11' 'httpx>=0.27' 'pillow>=12.0'
python -m pip install -i https://pypi.org/simple 'sherpa-onnx>=1.13' 'pilk>=0.2'
python -m pip install 'sqlite-vec>=0.1' 'paddlepaddle>=3.0' 'paddleocr>=3.7'
cp -n .env.example .env
```

Keep your existing `.env` if present. Set the model server address, key and model name:

```dotenv
OMLX_BASE_URL=http://127.0.0.1:8000
OMLX_API_KEY=1234
OMLX_MODEL=Qwen3.5-9B-4bit
```

Install omlx and load a vision-language model separately. The client uses the OpenAI-compatible `/v1/chat/completions` endpoint; compatibility with other servers needs verification. Do not append `/v1` to `OMLX_BASE_URL`.

### 2. Try it, then connect WeChat

```bash
# Start your configured local model server
omlx start

# Check screenshot understanding
python -m glimpsely.main test-llm

# Try recording and simulated pushes without logging into WeChat
python -m glimpsely.main demo

# Log in and run the assistant
python -m glimpsely.main run
```

Follow the terminal QR login instructions, then send a screenshot from your own WeChat account to the bot. After the save acknowledgement, ask a question or request a brief. The demo uses a separate `data/demo.db` and prints pushes to the terminal.

<details>
<summary>Optional: voice input, configuration and development</summary>

Voice input uses sherpa-onnx and SenseVoice. Place a matching SenseVoice ONNX model and token file at `models/sensevoice/model.int8.onnx` and `models/sensevoice/tokens.txt`, or set `ASR_MODEL` and `ASR_TOKENS`. Text and images work without these model files. PaddleOCR may download its recognition models on first use.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DIGEST_HOUR` | `21` | Automatic brief check hour, using the computer's local time |
| `MEMORY_TTL_DAYS` | `3` | Days before records become temporarily inactive |
| `PUSH_LIMIT_PER_HOUR` | `3` | Hourly proactive push limit |
| `PUSH_DAILY_CAP` | `15` | Daily proactive push limit |

```bash
python -m glimpsely.main login         # Log in again
python -m pip install ruff pytest pytest-asyncio
make ci                               # Lint and tests; some require local models
```

Defaults: `data/glimpsely.db` for memories, `data/media/` for media, and `data/run.log` for logs. See [DESIGN.md](DESIGN.md) for architecture and historical design notes in Chinese; current code and the limitations below take precedence.

</details>

## Current limitations

An early project for personal experimentation and learning:

- **One person's memory store.** Records are not isolated by user. Do not share an instance across users; the current integration supports one-to-one WeChat chats.
- **Reminders need a running process and a valid WeChat session.** A normal incoming message can restore an expired session. Recognition and reminders can miss information; verify important details against the original.
- **Forgetting is not deletion.** After 3 days by default, records stop participating in proactive reminders but remain locally searchable and can be reactivated. Reactivation may recalculate deadlines. Clearing memories does not remove media files, profiles or all indexes.
- **Automatic briefs have a known limitation.** The sent marker is not reset each day, so automatic daily repetition stops after the first success. Request a brief manually instead. `DIGEST_MINUTE` is not yet used by the scheduler.
- **Retrieval is lightweight.** It currently uses local character/token hash vectors, falling back to recent records if the vector extension is unavailable. Complex semantic queries may miss relevant records.

## License

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE):

- Noncommercial use, modification and redistribution of original or modified copies are permitted under the license.
- Redistribution must include the license text or its URL and preserve the original-author attribution in [NOTICE](NOTICE).
- This license does not grant commercial use rights; obtain separate permission from the author for commercial use.
- The license also explicitly permits certain uses by noncommercial organizations; see the [official terms](https://polyformproject.org/licenses/noncommercial/1.0.0). Third-party dependencies and models retain their own licenses.

This is **noncommercial, source-available software**, not open source under the OSI definition.
