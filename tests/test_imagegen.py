"""angler/imagegen.py: the ComfyUI workflow it builds, and one edit against a fake ComfyUI."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from angler import config, imagegen
from angler.imagegen import ComfyClient, ImageGenError
from tests import conftest
from tests.support import png

BASE = {"comfy_url": "http://127.0.0.1:1", "unet": "flux-2-klein-4b.safetensors", "text_encoder": "qwen_3_4b.safetensors",
        "vae": "flux2-vae.safetensors", "width": 768, "height": 512, "steps": 6, "cfg": 2.5, "reference_megapixels": 0.5,
        "tiled_decode": True, "tile_size": 384}


def build(**over) -> dict:
    return ComfyClient({**BASE, **over}).build("src.png", "a fox in snow", 1234)


# ---------- build() ----------
def test_build_safetensors_uses_comfys_own_loaders():
    wf = build()
    assert wf["1"] == {"class_type": "UNETLoader", "inputs": {"unet_name": "flux-2-klein-4b.safetensors", "weight_dtype": "default"}}
    assert wf["2"] == {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "flux2"}}


def test_build_gguf_uses_the_gguf_loaders():
    wf = build(unet="flux-2-klein-9b-Q8_0.gguf", text_encoder="qwen_3_8b-Q8_0.GGUF")
    assert wf["1"] == {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux-2-klein-9b-Q8_0.gguf"}}
    assert wf["2"] == {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": "qwen_3_8b-Q8_0.GGUF", "type": "flux2"}}


def test_build_mixed_loaders():
    wf = build(unet="model.gguf", text_encoder="enc.safetensors")
    assert wf["1"]["class_type"] == "UnetLoaderGGUF" and wf["2"]["class_type"] == "CLIPLoader"


def test_build_fills_everything_in():
    wf = build()
    assert not any(k.startswith("_") for k in wf)
    assert wf["3"]["inputs"]["vae_name"] == "flux2-vae.safetensors"
    assert wf["4"]["inputs"]["text"] == "a fox in snow"
    assert wf["5"]["inputs"]["text"] == ""  # the negative stays empty
    assert wf["20"]["inputs"]["image"] == "src.png"
    assert wf["21"]["inputs"]["megapixels"] == 0.5
    assert (wf["6"]["inputs"]["width"], wf["6"]["inputs"]["height"]) == (768, 512)
    assert wf["9"]["inputs"] == {"steps": 6, "width": 768, "height": 512}
    assert wf["10"]["inputs"]["cfg"] == 2.5
    assert wf["7"]["inputs"]["noise_seed"] == 1234
    assert wf["13"] == {"class_type": "PreviewImage", "inputs": {"images": ["12", 0]}}  # into ComfyUI's temp folder


def test_build_tiled_decode():
    wf = build(tiled_decode=True, tile_size=384)
    assert wf["12"]["class_type"] == "VAEDecodeTiled"
    assert wf["12"]["inputs"]["tile_size"] == 384
    assert wf["12"]["inputs"]["samples"] == ["11", 0] and wf["12"]["inputs"]["vae"] == ["3", 0]


def test_build_plain_decode():
    wf = build(tiled_decode=False)
    assert wf["12"] == {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}}
    assert wf["13"]["inputs"]["images"] == ["12", 0]


def test_build_defaults_from_the_example_config(home):
    image = config.load()["image"]
    wf = ComfyClient(image).build("s.png", "p", 1)
    assert wf["1"]["class_type"] == "UNETLoader" and wf["2"]["class_type"] == "CLIPLoader"
    assert wf["12"]["class_type"] == "VAEDecodeTiled"


def test_build_does_not_leak_between_calls():
    a = build(tiled_decode=False, unet="a.gguf")
    b = build()
    assert a["12"]["class_type"] == "VAEDecode" and b["12"]["class_type"] == "VAEDecodeTiled"
    assert b["1"]["class_type"] == "UNETLoader"


def test_workflow_graph_is_connected():
    """Every node another node points at exists, and the graph ends in PreviewImage: nothing of
    baitbook's is saved in ComfyUI's output folder."""
    for over in ({}, {"tiled_decode": False}, {"unet": "u.gguf", "text_encoder": "t.gguf"}):
        wf = build(**over)
        for nid, node in wf.items():
            assert "class_type" in node and "inputs" in node
            for value in node["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    assert value[0] in wf, f"node {nid} points at missing node {value[0]}"
        assert any(n["class_type"] == "PreviewImage" for n in wf.values())
        assert not any(n["class_type"] == "SaveImage" for n in wf.values())


# ---------- ensure_running: never starts anything it can't ----------
@pytest.fixture
def real_ensure(monkeypatch):
    monkeypatch.setattr(ComfyClient, "ensure_running", conftest.REAL_ENSURE_RUNNING)

    def no_popen(*a, **k):
        raise AssertionError("tried to start ComfyUI")

    monkeypatch.setattr(imagegen.subprocess, "Popen", no_popen)


def test_ensure_running_autostart_off(real_ensure):
    with pytest.raises(ImageGenError, match="autostart is off"):
        ComfyClient({**BASE, "comfy_autostart": False}).ensure_running()


def test_ensure_running_without_a_comfy_folder(real_ensure, home):
    with pytest.raises(ImageGenError, match="comfy_dir / comfy_python"):
        ComfyClient({**BASE, "comfy_dir": "", "comfy_python": ""}).ensure_running()
    (home / "ComfyUI").mkdir()
    with pytest.raises(ImageGenError, match="comfy_dir / comfy_python"):
        ComfyClient({**BASE, "comfy_dir": "ComfyUI", "comfy_python": "nope.exe"}).ensure_running()


def test_ensure_running_when_up_does_nothing(real_ensure, monkeypatch):
    monkeypatch.setattr(ComfyClient, "alive", lambda self: True)
    ComfyClient(BASE).ensure_running()


def test_release_with_nothing_started_and_nothing_up():
    assert imagegen.stop_started_comfy() is False
    ComfyClient(BASE).release()  # alive() is False in the tests: nothing to do


# ---------- edit() against a fake ComfyUI ----------
class FakeComfyUI(BaseHTTPRequestHandler):
    """Just enough of ComfyUI's API: /system_stats, /upload/image, /prompt, /history, /view."""

    state: dict = {}

    def log_message(self, *args):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        st = self.state
        url = urlparse(self.path)
        st["requests"].append(("GET", url.path))
        if url.path == "/system_stats":
            return self._json({"system": {}})
        if url.path.startswith("/history/"):
            pid = url.path.rsplit("/", 1)[1]
            st["polls"] += 1
            if st["polls"] < st["ready_after"]:
                return self._json({})
            return self._json({pid: st["history"]})
        if url.path == "/view":
            st["view"] = parse_qs(url.query)
            body = st["result"]
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        st = self.state
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        st["requests"].append(("POST", self.path))
        if self.path == "/upload/image":
            st["upload_type"] = self.headers.get("Content-Type", "")
            st["upload"] = body
            return self._json({"name": "uploaded_src.png", "subfolder": "", "type": "temp" if b'name="type"\r\n\r\ntemp' in body else "input"})
        if self.path == "/prompt":
            st["prompt"] = json.loads(body)
            if st.get("reject"):
                return self._json({"error": "bad node"}, 400)
            return self._json({"prompt_id": "p-123", "number": 1})
        if self.path == "/free":
            st["freed"] = json.loads(body)
            return self._json({})
        self._json({"error": "not found"}, 404)


@pytest.fixture
def comfyui(monkeypatch):
    """A fake ComfyUI on a free loopback port, and the real ComfyClient pointed at it."""
    result = png((1, 2, 3), (8, 8))
    FakeComfyUI.state = {"requests": [], "polls": 0, "ready_after": 1, "result": result,
                         "history": {"status": {"status_str": "success", "completed": True},
                                     "outputs": {"13": {"images": [{"filename": "ComfyUI_temp_abcde_00001_.png", "subfolder": "", "type": "temp"}]}}}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfyUI)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    monkeypatch.setattr(ComfyClient, "alive", conftest.REAL_ALIVE)
    monkeypatch.setattr(ComfyClient, "ensure_running", conftest.REAL_ENSURE_RUNNING)
    real_sleep = imagegen.time.sleep
    monkeypatch.setattr(imagegen.time, "sleep", lambda s: real_sleep(min(s, 0.01)))  # don't wait a second per /history poll
    client = ComfyClient({**BASE, "comfy_url": f"http://127.0.0.1:{server.server_address[1]}/"})
    try:
        yield client, FakeComfyUI.state
    finally:
        server.shutdown()
        server.server_close()


def test_edit_round_trip(comfyui):
    client, st = comfyui
    source = png((200, 100, 50), (10, 10))
    st["ready_after"] = 3  # two empty /history answers before the result
    out = client.edit(source, "a fox in snow", 77)
    assert out == st["result"]
    assert st["upload_type"].startswith("multipart/form-data; boundary=")
    assert source in st["upload"] and b'name="image"; filename="bb_src_' in st["upload"]
    assert b'name="overwrite"' in st["upload"] and b'name="type"\r\n\r\ntemp' in st["upload"]  # ComfyUI's temp folder, not its input folder
    wf = st["prompt"]["prompt"]
    assert st["prompt"]["client_id"] == "baitbook"
    assert wf["20"]["inputs"]["image"] == "uploaded_src.png [temp]"  # the name ComfyUI gave the upload, and where LoadImage finds it
    assert wf["4"]["inputs"]["text"] == "a fox in snow" and wf["7"]["inputs"]["noise_seed"] == 77
    assert st["polls"] == 3
    assert st["view"] == {"filename": ["ComfyUI_temp_abcde_00001_.png"], "type": ["temp"]}
    assert ("GET", "/system_stats") in st["requests"]


def test_edit_run_error(comfyui):
    client, st = comfyui
    st["history"] = {"status": {"status_str": "error", "completed": False, "messages": [["execution_error", {"node": "1"}]]}}
    with pytest.raises(ImageGenError, match="ComfyUI run failed"):
        client.edit(png(), "p", 1)


def test_edit_finished_without_an_image(comfyui):
    client, st = comfyui
    st["history"] = {"status": {"status_str": "success", "completed": True}, "outputs": {}}
    with pytest.raises(ImageGenError, match="without an image"):
        client.edit(png(), "p", 1)


def test_edit_prompt_rejected(comfyui):
    client, st = comfyui
    st["reject"] = True
    with pytest.raises(ImageGenError, match="/prompt failed"):
        client.edit(png(), "p", 1)


def test_edit_times_out(comfyui):
    client, st = comfyui
    st["ready_after"] = 10**9
    with pytest.raises(ImageGenError, match="Timed out"):
        client.edit(png(), "p", 1, timeout_s=0.2)


def test_release_asks_a_comfyui_it_did_not_start_to_free(comfyui):
    client, st = comfyui
    client.release()
    assert st["freed"] == {"unload_models": True, "free_memory": True}


def test_alive(comfyui, monkeypatch):
    client, _ = comfyui
    assert client.alive() is True

    def refused(*a, **k):
        raise imagegen.urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(imagegen.urllib.request, "urlopen", refused)
    assert client.alive() is False
