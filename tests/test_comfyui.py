"""baitbook's own ComfyUI (baitbook/comfyui.py) and the GPU detection behind it
(baitbook/gpu.py). Nothing is downloaded or installed: the network, uv and the
new environment's Python are all faked."""

from __future__ import annotations

import io
import zipfile

import pytest

from angler import config, imagegen
from baitbook import cli, comfyui, gpu
from baitbook.gpu import Gpu


# ---------- GPU detection ----------
@pytest.mark.parametrize("value, kind", [
    ("nvidia", "nvidia"), ("cuda", "nvidia"), ("cu124", "nvidia"), ("hip", "amd"), ("hip-radeon", "amd"),
    ("rocm72", "amd"), ("metal", "mps"), ("mps", "mps"), ("xpu", "intel"), ("cpu", "cpu"), ("vulkan", None), ("", None),
])
def test_baitbook_gpu_takes_the_install_scripts_names(monkeypatch, value, kind):
    monkeypatch.setenv("BAITBOOK_GPU", value)
    assert gpu.forced() == kind


def test_a_forced_kind_keeps_what_the_hardware_says_when_it_agrees(monkeypatch):
    monkeypatch.setattr(gpu, "detect_hardware", lambda: Gpu("amd", "Radeon RX 7900 XTX", arch="gfx1100"))
    monkeypatch.setenv("BAITBOOK_GPU", "hip")
    assert gpu.detect().arch == "gfx1100"
    monkeypatch.setenv("BAITBOOK_GPU", "cpu")
    assert gpu.detect().kind == "cpu"


@pytest.mark.parametrize("name, arch", [
    ("AMD Radeon(TM) 8060S Graphics", "gfx1151"), ("AMD Radeon RX 7900 XTX", "gfx1100"), ("AMD Radeon RX 7800 XT", "gfx1101"),
    ("AMD Radeon RX 9070 XT", "gfx1201"), ("AMD Radeon RX 9060 XT", "gfx1200"), ("AMD Radeon(TM) 890M Graphics", "gfx1150"),
    ("AMD Radeon RX 6800 XT", "gfx1030"), ("AMD Radeon RX 580", None),
])
def test_amd_architecture_from_the_name(name, arch):
    assert gpu.amd_arch(name) == arch


def test_nvidia_smi_is_read(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda name: "nvidia-smi")
    outputs = {True: "NVIDIA GeForce RTX 4090, 8.9\n",
               False: "| NVIDIA-SMI 580.95   Driver Version: 580.95   CUDA Version: 13.0 |"}
    monkeypatch.setattr(gpu, "_run", lambda cmd: outputs[len(cmd) > 1])
    found = gpu._nvidia()
    assert (found.kind, found.name, found.cuda, found.compute) == ("nvidia", "NVIDIA GeForce RTX 4090", (13, 0), (8, 9))


# ---------- which PyTorch ----------
def test_torch_for_nvidia_follows_the_driver():
    index = lambda g: comfyui.torch_args(g)[-1].rsplit("/", 1)[-1]  # noqa: E731
    assert index(Gpu("nvidia", cuda=(13, 0), compute=(8, 9))) == "cu130"
    assert index(Gpu("nvidia", cuda=(12, 9), compute=(8, 9))) == "cu128"
    assert index(Gpu("nvidia", cuda=(12, 6), compute=(8, 9))) == "cu126"
    assert index(Gpu("nvidia", cuda=(13, 0), compute=(6, 1))) == "cu126"  # a 10 series card
    with pytest.raises(comfyui.InstallError, match="Update the driver"):
        comfyui.torch_args(Gpu("nvidia", cuda=(12, 2), compute=(8, 6)))


def test_torch_for_amd_on_windows_installs_just_this_gpus_kernels(monkeypatch):
    monkeypatch.setattr(comfyui.sys, "platform", "win32")
    args = comfyui.torch_args(Gpu("amd", "Radeon RX 7900 XTX", arch="gfx1100"))
    assert args[:2] == ["--index-url", comfyui.AMD_WINDOWS]
    assert "torch[device-gfx1100]==2.13.0+rocm10.0.0" in args
    assert "torch[device-all]==2.13.0+rocm10.0.0" in comfyui.torch_args(Gpu("amd", "Radeon something"))


def test_torch_for_the_rest(monkeypatch):
    monkeypatch.setattr(comfyui.sys, "platform", "linux")
    assert comfyui.torch_args(Gpu("amd"))[-1].endswith("/rocm7.2")
    assert comfyui.torch_args(Gpu("intel"))[-1].endswith("/xpu")
    assert comfyui.torch_args(Gpu("cpu"))[-1].endswith("/cpu")
    assert comfyui.torch_args(Gpu("mps")) == ["torch", "torchvision", "torchaudio"]


# ---------- small pieces ----------
def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_unzip_returns_the_top_folder_and_refuses_paths_outside(tmp_path):
    top = comfyui.unzip(make_zip({"ComfyUI-1.0/main.py": "", "ComfyUI-1.0/requirements.txt": ""}), tmp_path)
    assert top == tmp_path / "ComfyUI-1.0" and (top / "main.py").is_file()
    with pytest.raises(comfyui.InstallError, match="unsafe path"):
        comfyui.unzip(make_zip({"x/../../evil.txt": ""}), tmp_path / "inner")


def test_models_dir(home):
    assert comfyui.models_dir({"comfy_base": "", "comfy_dir": ""}) is None
    assert comfyui.models_dir({"comfy_base": "", "comfy_dir": "C"}) == home / "C" / "models"
    assert comfyui.models_dir({"comfy_base": "B", "comfy_dir": "C"}) == home / "B" / "models"


def test_needs_gguf():
    assert comfyui.needs_gguf({"unet": "klein.GGUF", "text_encoder": "qwen.safetensors"})
    assert not comfyui.needs_gguf({"unet": "klein.safetensors", "text_encoder": "qwen.safetensors"})


# ---------- install(), with everything outside faked ----------
@pytest.fixture
def fake_install(monkeypatch, home):
    """No network, no uv: `run` records the commands, and the venv command makes a fake python."""
    calls, downloads = [], []
    code_zip = make_zip({f"ComfyUI-{comfyui.VERSION}/main.py": "", f"ComfyUI-{comfyui.VERSION}/requirements.txt": "torch\n"})
    nodes_zip = make_zip({"ComfyUI-GGUF-abc/__init__.py": "", "ComfyUI-GGUF-abc/requirements.txt": "gguf\n"})

    def download(url):
        downloads.append(url)
        return nodes_zip if "GGUF" in url else code_zip

    def run(cmd, what):
        calls.append(what)
        if cmd[1] == "venv":
            py = comfyui.venv_python(comfyui.root() / ".venv")
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")

    monkeypatch.setattr(comfyui, "download", download)
    monkeypatch.setattr(comfyui, "run", run)
    monkeypatch.setattr(comfyui, "find_uv", lambda: "uv")
    state = {"torch": False}
    monkeypatch.setattr(comfyui, "gpu_check", lambda python: (True, "2.13 fake") if state["torch"] else (False, "No module named 'torch'"))
    original = run

    def run_and_track(cmd, what):
        original(cmd, what)
        if what.startswith("PyTorch"):
            state["torch"] = True

    monkeypatch.setattr(comfyui, "run", run_and_track)
    return calls, downloads


def test_install_sets_everything_up_and_points_the_config_at_it(fake_install, home):
    calls, downloads = fake_install
    code = comfyui.install(Gpu("cpu"))
    root = home / "comfyui"
    assert code == root / f"ComfyUI-{comfyui.VERSION}" and (code / "main.py").is_file()
    assert calls == [f"a Python {comfyui.PYTHON} environment", "PyTorch for this GPU", "ComfyUI's requirements"]
    assert all((root / "models" / f).is_dir() for f in comfyui.MODEL_FOLDERS)
    image = config.section("image")
    assert config.path(image["comfy_dir"]) == code and config.path(image["comfy_base"]) == root
    assert config.path(image["comfy_python"]) == comfyui.venv_python(root / ".venv")
    assert len(downloads) == 1  # klein 4B safetensors by default: no GGUF nodes


def test_install_again_skips_what_is_done(fake_install, home):
    calls, downloads = fake_install
    comfyui.install(Gpu("cpu"))
    calls.clear(), downloads.clear()
    comfyui.install(Gpu("cpu"))
    assert downloads == [] and calls == ["ComfyUI's requirements"]


def test_install_adds_the_gguf_nodes_for_gguf_models_and_drops_old_versions(fake_install, home):
    calls, downloads = fake_install
    config.load()
    config.update({"image": {"unet": "flux-2-klein-9b-Q8_0.gguf"}})
    old = home / "comfyui" / "ComfyUI-0.1.0"
    old.mkdir(parents=True)
    (old / "main.py").write_text("")
    comfyui.install(Gpu("cpu"))
    assert (home / "comfyui" / "custom_nodes" / "ComfyUI-GGUF" / "__init__.py").is_file()
    assert "the ComfyUI-GGUF nodes' requirements" in calls
    assert not old.exists()


def test_the_angler_starts_comfyui_with_its_base_directory(monkeypatch, home):
    monkeypatch.setattr(imagegen.ComfyClient, "ensure_running", __import__("tests.conftest", fromlist=["x"]).REAL_ENSURE_RUNNING)
    code, py = home / "comfyui" / "ComfyUI", home / "comfyui" / "py.exe"
    code.mkdir(parents=True)
    (code / "main.py").write_text("")
    py.write_text("")
    started = []

    class Proc:
        def poll(self):
            return None

    def popen(cmd, **kw):
        started.append(cmd)
        return Proc()

    monkeypatch.setattr(imagegen.subprocess, "Popen", popen)
    alive = iter([False, True])
    monkeypatch.setattr(imagegen.ComfyClient, "alive", lambda self: next(alive))
    client = imagegen.ComfyClient({"comfy_url": "http://127.0.0.1:8189", "comfy_dir": str(code), "comfy_python": str(py),
                                   "comfy_base": str(home / "comfyui"), "comfy_log": str(home / "comfy.log")})
    client.ensure_running()
    assert started[0][-2:] == ["--base-directory", str(home / "comfyui")]
    imagegen._PROC = None


def test_cli_comfyui_waits_for_the_angler_to_stop(monkeypatch, capsys):
    monkeypatch.setattr(cli, "call", lambda *a, **k: {"engine": {"state": "idle"}})
    with pytest.raises(SystemExit) as stop:
        cli.main(["comfyui"])
    assert stop.value.code == 1 and "baitbook stop" in capsys.readouterr().out


# ---------- someone else's ComfyUI on the port ----------
def test_comfy_url_avoids_8188_and_busy_ports(monkeypatch):
    busy = {8189, 8190}
    monkeypatch.setattr(comfyui, "port_free", lambda port: port not in busy)
    assert comfyui.comfy_url("http://127.0.0.1:8188") == "http://127.0.0.1:8191"
    assert comfyui.comfy_url("http://127.0.0.1:8189") == "http://127.0.0.1:8191"
    assert comfyui.comfy_url("http://127.0.0.1:8195") == "http://127.0.0.1:8195"


class Stats:
    def __init__(self, argv):
        self.body = __import__("json").dumps({"system": {"argv": argv}}).encode()

    def __enter__(self):
        return io.BytesIO(self.body)

    def __exit__(self, *a):
        return False


def client_with_base(home):
    return imagegen.ComfyClient({"comfy_url": "http://127.0.0.1:8190", "comfy_base": str(home / "comfyui")})


def test_ours_goes_by_the_base_directory(monkeypatch, home):
    c = client_with_base(home)
    monkeypatch.setattr(imagegen.urllib.request, "urlopen", lambda *a, **k: Stats(["main.py", "--base-directory", str(home / "comfyui")]))
    assert c.ours()
    monkeypatch.setattr(imagegen.urllib.request, "urlopen", lambda *a, **k: Stats(["main.py", "--port", "8190"]))
    assert not c.ours()
    assert imagegen.ComfyClient({"comfy_url": "http://127.0.0.1:8190", "comfy_base": ""}).ours()  # your own ComfyUI: any will do


def test_someone_elses_comfyui_is_never_used_or_freed(monkeypatch, home):
    from tests import conftest

    monkeypatch.setattr(imagegen.ComfyClient, "ensure_running", conftest.REAL_ENSURE_RUNNING)
    monkeypatch.setattr(imagegen.ComfyClient, "alive", lambda self: True)
    monkeypatch.setattr(imagegen.ComfyClient, "ours", lambda self: False)
    posted = []
    monkeypatch.setattr(imagegen.ComfyClient, "_post", lambda self, path, payload: posted.append(path))
    c = client_with_base(home)
    with pytest.raises(imagegen.ImageGenError, match="Another ComfyUI"):
        c.ensure_running()
    c.release()
    assert posted == []


def test_a_failed_download_is_an_install_error(monkeypatch):
    def offline(*a, **k):
        raise comfyui.urllib.error.URLError("no network")

    monkeypatch.setattr(comfyui.urllib.request, "urlopen", offline)
    with pytest.raises(comfyui.InstallError, match="run `baitbook comfyui` again"):
        comfyui.download("https://example.invalid/ComfyUI.zip")
