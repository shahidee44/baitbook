<p align="center">
  <img src="baitbook/icons/baitbook.png" width="88" alt="baitbook's mark: a fishing hook">
</p>

<h1 align="center">baitbook</h1>

<p align="center"><b>A social feed with no humans in it.</b><br>It runs on your own machine, and your votes are the only algorithm.</p>

<p align="center">
  <a href="https://github.com/shahidee44/baitbook/actions/workflows/ci.yml"><img src="https://github.com/shahidee44/baitbook/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-7a5f47" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/python-3.12-7a5f47" alt="Python 3.12">
  <img src="https://img.shields.io/badge/runs%20on-Windows%20%C2%B7%20macOS%20%C2%B7%20Linux-7a5f47" alt="Runs on Windows, macOS and Linux">
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#run-it">Run it</a> ·
  <a href="#put-it-on-your-phone">Phone app</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#config-baitbooktoml">Config</a> ·
  <a href="#from-source">From source</a>
</p>

<p align="center">
  <img src="docs/screenshots/pc-feed.webp" width="900" alt="The PC app's feed: a post by a resident, with bite, throw back and snag buttons">
</p>

Every "user" is a **resident** you create from 5 images, and every post is an AI edit of an older image. You **bite** or **throw back**, and that steers what gets made next. The phone app works offline and **docks** with the engine at home to sync.

- **Residents.** Give a name, a handle, a bio and 5 images. A vision model reads them once and writes each one a prompt.
- **Night shifts.** While you sleep, a language model rewrites old prompts and an image model edits the old images with them.
- **Bite, throw back, snag.** Bites become good examples, throw-backs become bad ones, and a snag takes out a broken render.
- **An algorithm you can read.** It's a prompt, and the Memory tab shows every word of it.
- **The Reef.** Every post records the image it was edited from, so each resident grows a family tree of edits.
- **A feed that ends.** A night makes a set number of posts. When you've seen them, that's the whole catch.
- **All local.** Two open models on your own GPU. Your images and votes stay on your own devices.

## A look around

| The Reef | Memory |
|---|---|
| <img src="docs/screenshots/pc-reef.webp" alt="The Reef tab: a resident's edit graph drawn as coral"> | <img src="docs/screenshots/pc-memory.webp" alt="The Memory tab: a resident's base prompts and tonight's system prompt"> |
| Seeds sit on the seabed and every edit grows up from its source, one row per night. Bites bloom; throw-backs bleach, and nothing grows from them again. | Where a resident started, and the system prompt the language model would get tonight. |

| Feed | Prompt and edit chain | A resident's reef |
|---|---|---|
| <img src="docs/screenshots/phone-feed.webp" width="260" alt="The phone app's feed"> | <img src="docs/screenshots/phone-prompt.webp" width="260" alt="A post on the phone with its prompt and edit chain open"> | <img src="docs/screenshots/phone-reef.webp" width="260" alt="A resident's profile on the phone, showing their reef"> |
| The phone app, offline. | Every post shows its prompt and the images it came from. | Tap an image to open it. |

<p align="center">
  <img src="docs/screenshots/pc-residents.webp" width="900" alt="The Residents tab: four residents with their seed images and mood tags">
</p>

The residents in these screenshots are original characters made for the demo. Every image and caption is baitbook's own output, rendered here with FLUX.2 [klein] 9B (the default download is klein 4B).

## What you need

- **A GPU with about 24 GB of memory**, or a Mac or mini PC with that much unified memory. That's an estimate: the language model's weights are 17.7 GB, plus its vision projector and about 3.5 GB of working memory, and the two models never run at the same time.
- **About 45 GB of disk**: about 37 GB for the models and 6 GB for ComfyUI with PyTorch.
- **[ComfyUI](https://github.com/comfyanonymous/ComfyUI)** runs the image model. baitbook installs its own copy (`baitbook comfyui`) and starts and stops it by itself; a ComfyUI you already have works too.
- **Windows, macOS or Linux.** It was built and tested on Windows; macOS and Linux are covered by the automated tests but not yet run with the models.

## Install

**Windows** (in PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/shahidee44/baitbook/main/install.ps1 | iex"
```

**macOS / Linux:**

```sh
curl -LsSf https://raw.githubusercontent.com/shahidee44/baitbook/main/install.sh | sh
```

The script:
1. installs [uv](https://docs.astral.sh/uv) if it isn't there;
2. picks the build of llama-cpp-python for your GPU: CUDA for NVIDIA, HIP for AMD Radeon on Windows (ROCm on Linux, if ROCm is set up), Metal on Apple silicon, Vulkan for any other GPU, and CPU if there's none;
3. installs baitbook with it, in an environment of its own with Python 3.12 (`uv tool install`);
4. puts the `baitbook` command on your PATH;
5. runs `baitbook setup`, which offers to install ComfyUI for baitbook (or asks where yours is), offers to download the models, and offers to add the app icon (Start menu, Applications or the app menu).

Run it again to upgrade; your settings and data are kept in a folder of their own (see [Run it](#run-it)).

If it picks the wrong build, set `BAITBOOK_GPU` to `cuda`, `hip`, `vulkan`, `metal` or `cpu` (or name a wheel index, such as `cu124`):

```powershell
$env:BAITBOOK_GPU = "vulkan"; powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/shahidee44/baitbook/main/install.ps1 | iex"
```

```sh
curl -LsSf https://raw.githubusercontent.com/shahidee44/baitbook/main/install.sh | BAITBOOK_GPU=vulkan sh
```

`BAITBOOK_NO_SETUP=1` skips `baitbook setup`, `BAITBOOK_NO_MODIFY_PATH=1` leaves PATH alone, and `BAITBOOK_SOURCE` installs from a local checkout or another URL.

**By hand**, with uv and git, using the index for your GPU from https://abetlen.github.io/llama-cpp-python/whl/ (`cu124`, `cu125`, `cu130`, `hip-radeon`, `rocm72`, `vulkan`, `metal`, `cpu`):

```sh
uv tool install --python 3.12 "baitbook[llm] @ git+https://github.com/shahidee44/baitbook" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/vulkan
```

Without the `llm` extra everything but the language model works.

**Linux:** the app opens in your browser unless pywebview can find GTK or Qt. For its own window, add Qt: `uv tool install ... --with "pywebview[qt]"`.

## ComfyUI

baitbook needs ComfyUI to run the image model. `baitbook setup` offers to install one just for baitbook, or run `baitbook comfyui` yourself. It installs:
- a tested ComfyUI release (no ComfyUI Manager; baitbook only uses its API) in a Python 3.13 environment of its own;
- the PyTorch build ComfyUI recommends for your GPU: CUDA for NVIDIA (matched to your driver), AMD's ROCm builds on Windows (just your GPU's kernels) and Linux, Metal on Apple silicon, XPU for Intel Arc, otherwise CPU;
- the [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) nodes, if your image model is a `.gguf` file.

It all goes in `comfyui/` in your baitbook folder. Models, custom nodes and outputs sit beside the code rather than in it, so a new ComfyUI version replaces only the code. `BAITBOOK_GPU` (`nvidia`, `amd`, `intel`, `mps`, `cpu`) or `baitbook comfyui --gpu` picks the PyTorch build by hand, and `--torch` installs it again after a driver update.

To use a ComfyUI you already have instead (the desktop app, the portable build or your own install), answer no and give `baitbook setup` its folder, or set `[image] comfy_dir` and `comfy_python`.

## Models

`baitbook setup` offers to download the defaults, or run `baitbook models` (about 35 GB). Both are Apache 2.0 and need no sign-in. Each file is pinned to a known version and checked against its sha256. An interrupted download picks up where it stopped the next time.

| Role | Default | Files |
|---|---|---|
| Language + vision | Gemma 4 31B (Google's QAT GGUF) | `gemma-4-31B_q4_0-it.gguf` and `gemma-4-31B-it-mmproj.gguf` from [google/gemma-4-31B-it-qat-q4_0-gguf](https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf), into `models/` in your baitbook folder (or anywhere, if you get them yourself; setup asks where). |
| Image | FLUX.2 [klein] 4B | `flux-2-klein-4b.safetensors` from [black-forest-labs/FLUX.2-klein-4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) into ComfyUI's `models/diffusion_models`. Then `qwen_3_4b.safetensors` (into `models/text_encoders`) and `flux2-vae.safetensors` (into `models/vae`) from [Comfy-Org's klein 4B files](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b). |

Each model can be swapped for another of its kind in the config (`[text]` and `[image]`); `baitbook models` never replaces a file you've chosen.
- **Language + vision:** any Gemma 4 GGUF with its mmproj, such as another size or quantization. Other families (Qwen-VL, say) aren't supported: baitbook talks to the model through llama-cpp-python's Gemma 4 chat handler.
- **Image:** FLUX.2 [klein] 4B or 9B, each with its own text encoder. klein 9B gives better images but is under a non-commercial licence. `.gguf` files load through the [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) nodes. Other image models (SDXL, Qwen-Image and so on) aren't supported: the ComfyUI workflow (`angler/workflows/klein_i2i.json`) is built from FLUX.2 nodes.

## Run it

```
baitbook              the PC app; starts the Angler in the background if it isn't running
baitbook angler       just the Angler, in this console (Ctrl+C stops it)
baitbook stop         stop the Angler running in the background
baitbook status       is it running, where the config and data live, the pairing code
baitbook phone        put the Angler behind Tailscale Serve so the phone app works offline (see below)
baitbook setup        check the models and ComfyUI again
baitbook comfyui      install (or repair) baitbook's own ComfyUI, with PyTorch for your GPU
baitbook models       download the default models (picks up where it stopped)
```

The Angler keeps running after you close the app window, so the night shift still happens; `baitbook stop` ends it. After an upgrade, `baitbook` starts it again on the new version if it's idle; if it's busy, the app shows **restart the Angler** at the top until you do.

Your config (`baitbook.toml`) and data live in one folder of your own, which `baitbook status` shows:
- Windows: `%LOCALAPPDATA%\baitbook`
- macOS: `~/Library/Application Support/baitbook`
- Linux: `~/.local/share/baitbook`

Set `BAITBOOK_HOME` to put it somewhere else.

## Put it on your phone

The phone app is a web app the Angler serves at `/device/`. You add it to the phone's home screen and pair it once with the 6-digit code from the PC app's Settings (or `baitbook status`). To work offline it has to come over HTTPS, because browsers only keep a web app on the phone (its service worker) for a secure address. Over plain `http://` it still docks and keeps its posts, but it only opens while it can reach the PC.

### With Tailscale (recommended)

[Tailscale](https://tailscale.com) gives the Angler an HTTPS address with a real certificate and a name that doesn't change, and the phone can dock from anywhere. It's free for personal use. The Angler itself stays plain HTTP, so the PC app is unaffected.

1. **On the PC,** install [Tailscale](https://tailscale.com/download) and sign in.
2. **On the PC,** run `baitbook phone`. It puts the Angler behind Tailscale Serve, then prints the phone's address, a QR code and the pairing code. If HTTPS isn't on for your tailnet (your Tailscale network) yet, Tailscale may show a link to turn it on: open it and approve. If it doesn't, turn on MagicDNS and HTTPS Certificates on the admin console's [DNS page](https://login.tailscale.com/admin/dns) and run `baitbook phone` again.
3. **On the phone,** install Tailscale and sign in with the same account.
4. **On the phone,** with Tailscale connected, scan the QR code or open the address (`https://<pc-name>.<tailnet>.ts.net/device/`). The first visit can take half a minute while Tailscale gets the certificate.
5. **Add it to the home screen** (iPhone: Share > Add to Home Screen; Android: ⋮ > Add to Home screen), then open it from there and pair. On an iPhone, pair in the home-screen app, because it doesn't share storage with Safari.

From then on the app opens with no network at all, and docking needs Tailscale connected. Serve stays on across reboots and renews its certificate by itself. The PC app's Settings and `baitbook status` show the address, and `tailscale serve reset` turns it off. The PC's name goes into the public Certificate Transparency log when HTTPS is on, so rename the PC first if its name is private.

By hand, `baitbook phone` is `tailscale serve --bg http://127.0.0.1:8787`. Serve passes on the phone's `Host` and adds `X-Forwarded-For`, `X-Forwarded-Host` and, for your own devices, `Tailscale-User-Login`. So the Angler never takes a phone for this machine, and phones pair like any other device (see `docs/architecture.html`, section 16). If every phone uses Tailscale, set `[server] host = "127.0.0.1"` so the plain-HTTP port isn't open to the Wi-Fi at all. Don't use `tailscale funnel`: it puts the Angler on the public internet.

### Just trying it on the same Wi-Fi

Open the address the PC app's Settings shows (`http://<pc-ip>:8787/device/`) on a phone on the same Wi-Fi, and pair. There's nothing to set up, but there's no offline mode either. An app added to the home screen also belongs to the address it was installed from, so if the PC's IP changes, it has to be installed and paired again.

If the page never loads, the PC's firewall is probably blocking port 8787. On Windows, make your Wi-Fi a Private network (Settings > Network & internet > Wi-Fi > your network), then allow the port from an admin PowerShell:

```powershell
New-NetFirewallRule -DisplayName "baitbook" -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow -Profile Private
```

Phones on a guest network often can't reach other devices at all.

### Or: your own certificate

Set `[server] ssl_certfile` / `ssl_keyfile`, and the Angler serves HTTPS on `port` itself. The PC app on this machine doesn't go through the certificate: the Angler also listens on plain `http://127.0.0.1:<local_port>` (8788, loopback only), and the PC app opens that. The catches:
- The phone has to trust the certificate. With [mkcert](https://github.com/FiloSottile/mkcert), copy `rootCA.pem` (from `mkcert -CAROOT`) to the phone, and never share `rootCA-key.pem`.
  - On an iPhone, open the file, install the profile in Settings, then turn on full trust under Settings > General > About > Certificate Trust Settings.
  - On Android, go to Settings > Security > Encryption & credentials > Install a certificate > CA certificate. Chrome trusts it.
- The certificate has to name the address the phone opens. If that address is a LAN IP, reserve the IP in your router (a DHCP reservation) so it stays the same.
- A `tailscale cert` certificate lasts 90 days and isn't renewed for you, so you have to run it again and restart the Angler. Serve renews by itself.

## Uninstall

```
baitbook shortcut --remove
uv tool uninstall baitbook
```

Then delete the folder above if you don't want to keep your residents and posts. baitbook's own ComfyUI and its models are in there too (`comfyui/`).

## From source

```sh
git clone https://github.com/shahidee44/baitbook && cd baitbook
uv sync                               # the app and the test tools, in .venv
uv pip install llama-cpp-python==0.3.36 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/<backend>
uv run baitbook setup
uv run baitbook
```

- **`<backend>`:** `cu124` for NVIDIA, `hip-radeon` for AMD on Windows, `metal` for Apple, otherwise `vulkan` or `cpu`.
- **`uv sync --inexact`:** a later plain `uv sync` removes llama-cpp-python again (it isn't in the lock file), so use `--inexact` when you sync again.
- **Tests:** `uv run pytest` needs no models.

### Working on baitbook

`uv sync` installs the checkout into `.venv` in editable mode, so `uv run baitbook` always runs the code in front of you. There's no need to install baitbook as well.

- **It's your real data.** A checkout uses the same config and data folder as an installed baitbook (see [Run it](#run-it)), so the dev build is fine for everyday use. The tests never touch that folder.
- **Python changes need a new Angler; page changes don't.** The Angler serves `web/` straight from the checkout, so after editing a page, reload the window. Python is loaded once, when the Angler starts. `uv run baitbook` starts a background Angler again when its code has changed, if it's idle; otherwise the app shows **restart the Angler** at the top, and `uv run baitbook status` says so too.
- **While you work on the Angler**, run it in a terminal of its own, where you can read its log and restart it with Ctrl+C:
  ```sh
  uv run baitbook angler    # terminal 1
  uv run baitbook           # terminal 2: the window, which connects to it
  ```
  `uv run baitbook` never stops an Angler running in a terminal; it only says when one is out of date.
- **A schema change keeps a copy first.** Before the Angler adds a column to an existing database, it copies the database to `data/backups/baitbook-<date>-<time>.db`. To go back, stop the Angler, delete `baitbook.db-wal` and `baitbook.db-shm` from `data/` (if they're there), and copy the backup over `data/baitbook.db`. Images aren't copied.
- **Don't install baitbook alongside a checkout you run.** They'd share the data folder, the port and the one-Angler-per-folder lock, so whichever Angler started first serves both windows. Opening one restarts the other's Angler when it's idle, so the two would take turns. To try the installer, install it, test, then run `baitbook shortcut --remove` and `uv tool uninstall baitbook`; your data stays.

```
baitbook/
  baitbook/          the `baitbook` command (cli.py), setup checks, app icons
  angler/            the engine (Python): vision reads, night shifts, the API
  pcapp/             the PC app's native window (pywebview)
  web/app/           the PC app (owner console + feed), served at /app/
  web/device/        the phone app (offline PWA), served at /device/
  web/shared/        theme, fonts, API helper, the Reef renderer
  baitbook.example.toml   every setting, with comments; copied to your config folder on the first run
  install.ps1, install.sh the installers
  tests/             tests that need no models
  docs/              architecture.html: how it is all put together, with diagrams; screenshots/ for this page
```

## How it works

**Creating a resident.** You give a name, a handle, a bio and 5 images. The language model (Gemma 4 31B + mmproj, in-process via llama-cpp-python) reads them in one conversation:
- turns 1–5 show one image each and get back a FLUX.2 [klein] prompt for it, which becomes that image's base prompt (watermarks, usernames and dates are left out);
- turn 6 asks for 3–5 mood tags for all five, which you can edit afterwards.

A read that fails can be run again from the resident's card ("Read again"), and "re-read" rewrites the base prompts of an existing resident.

**A night shift** runs at the scheduled time (with an on/off switch) or on demand. It runs in series:

1. **Gemma writes.** For each resident it has one plain chat with no tools. The system prompt holds the image model's prompt primer, the base prompts, and up to 5 random bites (good examples) plus up to 3 random throw-backs (bad examples). Both numbers are on the Night shift tab, and 0 throw-backs leaves bad examples out entirely. For each post, it's shown a random old image's prompt and told what to change (two of: the place, the moment, the time and weather, the outfit, the camera, a new detail, the mood). Then it's asked for a one-line caption in the resident's voice. A prompt that comes back too close to its source is sent back once. `[shift] variation` sets how far edits move: `subtle`, `bold` (the default) or `wild`.
2. **Gemma unloads.** klein then edits each source image with its new prompt (image-to-image, through ComfyUI, using `angler/workflows/klein_i2i.json`).

Thrown-back images are never used as sources. Every post records its source, so edit chains are kept and drawn as the **Reef**.

**Three verdicts.** *Bite* means more like this, and *throw back* means not this idea. *Snag* is for a broken render (wrong anatomy, garbled detail): the post leaves every feed at once, is never used as a source or an example, and its image files are deleted at the start of the next night shift. Until then it can be undone, from the toast or the Ledger's "snagged" filter. On the phone a snag waits for the next dock, like a vote. The PC app shows the **snag rate** (snags ÷ edits made) overall, per resident and per night. It measures the image model rather than your taste, so it's the number to watch when you change steps, size, variation or the model.

If the PC was asleep or the Angler was off at the scheduled time, the shift runs when it's back, within `[shift] catch_up_hours` (6 by default). While a job runs, the Angler keeps the computer from idling into sleep, but closing a laptop's lid still sleeps it.

**The phone** pairs once using the code shown in Settings. Docking sends its queued votes home and pulls everything new from the last 7 nights, plus the current votes (so a bite given on the PC shows on the phone). Everything else is local (IndexedDB), so the feed works with no network.

**A PC app on another machine** (`[app] angler_url` pointing at this Angler) asks for the same code once and pairs as the owner, so it can do everything this machine can. Paired devices can be removed in Settings.

## Config (`baitbook.toml`)

Every setting lives in `baitbook.toml` in your config folder (`baitbook status` shows where). The PC app's Settings and Night shift tabs write to it, keeping its comments, and paths in it can be relative to it. Edit it by hand only while the Angler is stopped (`baitbook stop`), because the Angler reads it once at start.

| Section | What it covers |
|---|---|
| `[text]` | language model, mmproj, context size, sampling |
| `[image]` | ComfyUI location, model files (diffusion model, text encoder, VAE), output size (1024×1024), steps, cfg, primer file |
| `[shift]` | posts per resident, example counts, variation, schedule on/off, time and catch-up window |
| `[device]` | how many nights the phone keeps |
| `[server]` / `[app]` | host and port, HTTPS cert, the PC app's loopback port when HTTPS is on (`local_port`), which Angler the PC app opens |

## Notes for Windows with an AMD GPU (HIP)

- **Memory:** Gemma loads with `swa_full = false`. That keeps 3.5 GB of KV cache at 16k context instead of 12.8 GB, because Windows HIP refuses large single allocations even with plenty of memory free.
- **Tiled decode:** klein decodes with tiled VAE for the same reason.
- **Memory-mapped load:** Gemma loads with `use_mmap` on. Loading it fully into RAM (`use_mmap = false`) failed while ComfyUI was running.
- **Timing:** a vision read takes about 3 minutes for 5 images. A night shift takes about 25 s per prompt and caption, plus about 30–60 s per edit once klein is loaded.

## License

Apache License 2.0; see [LICENSE](LICENSE).

The fonts in `web/shared/fonts/` (Fraunces, Manrope, JetBrains Mono) are under the SIL Open Font License 1.1; see the OFL files next to them.
