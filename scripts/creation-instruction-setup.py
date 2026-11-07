"""One official fp16 UNet, byte-preserved local encoders; no model loading."""
from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import struct
import subprocess
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
REPO = "timbrooks/instruct-pix2pix"
REV = "31519b5cb02a7fd89b906d88731cd4d6a7bbf88d"
NAME = "unet/diffusion_pytorch_model.fp16.safetensors"
SHA = "0d6bbc0a95dd125196d327a660b43d24c56f433eb30d2776f1327fb86bd38f78"
SIZE = 1719148344
PREFIXES = ("first_stage_model.", "cond_stage_model.")


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def extract_encoders(source, destination):
    """Stream original tensor bytes, retaining names/dtypes; no framework or casting."""
    with source.open("rb") as incoming:
        length = struct.unpack("<Q", incoming.read(8))[0]
        if length > 20_000_000:
            raise ValueError("Unreasonable safetensors header")
        header = json.loads(incoming.read(length))
        tensor_base = 8 + length
        selected = {name: row for name, row in header.items() if name.startswith(PREFIXES)}
        if not selected or not all(any(name.startswith(prefix) for name in selected) for prefix in PREFIXES):
            raise ValueError("Both original encoder components are required")
        mapping, offset = [], 0
        out_header = {"__metadata__": {"format": "pt", "origin": "byte-preserved SD1.5 VAE and CLIP subset"}}
        for name, row in selected.items():
            start, end = row["data_offsets"]
            if not 0 <= start < end <= source.stat().st_size - tensor_base:
                raise ValueError("Invalid source tensor extent")
            out_header[name] = {**row, "data_offsets": [offset, offset + end - start]}
            mapping.append({"name": name, "dtype": row["dtype"], "shape": row["shape"],
                            "source_data_offsets": [start, end], "destination_data_offsets": out_header[name]["data_offsets"]})
            offset += end - start
        encoded = json.dumps(out_header, separators=(",", ":")).encode()
        encoded += b" " * (-len(encoded) % 8)
        partial = destination.with_suffix(".safetensors.part")
        with partial.open("wb") as outgoing:
            outgoing.write(struct.pack("<Q", len(encoded)))
            outgoing.write(encoded)
            for row in mapping:
                start, end = row["source_data_offsets"]
                incoming.seek(tensor_base + start)
                remaining = end - start
                while remaining:
                    block = incoming.read(min(16 * 1024 * 1024, remaining))
                    if not block:
                        raise ValueError("Truncated source tensor")
                    outgoing.write(block)
                    remaining -= len(block)
        partial.replace(destination)
    return {"tensor_count": len(mapping), "tensor_bytes": offset, "source_tensor_base": tensor_base,
            "destination_tensor_base": 8 + len(encoded), "mapping": mapping}


def fetch(url, path):
    subprocess.run(["curl.exe", "--fail", "--location", "--retry", "2", "--connect-timeout", "30",
                    "--max-time", "90", "--silent", "--show-error", "--output", str(path), url], check=True)


def main():
    folder = ROOT / "models/creation/instruct-pix2pix"
    folder.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < 4_000_000_000:
        raise RuntimeError("Need 4GB free for bounded candidate preparation")
    source_api = ROOT / "artifacts/creation-instruction-source/official-model-api.json"
    api = json.loads(source_api.read_text(encoding="utf-8-sig"))
    assert api["id"] == REPO and api["sha"] == REV and api["cardData"]["license"] == "mit"
    entries = {item["rfilename"]: item for item in api["siblings"]}
    assert entries[NAME]["size"] == SIZE and entries[NAME]["lfs"]["sha256"] == SHA
    readme = folder / "README.model.md"
    fetch(f"https://huggingface.co/{REPO}/resolve/{REV}/README.md", readme)
    raw = readme.read_bytes()
    assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == entries["README.md"]["blobId"]
    shutil.copyfile(ROOT / "artifacts/creation-instruction-source/official-LICENSE", folder / "LICENSE.instruct-pix2pix")
    shutil.copyfile(ROOT / "models/creation/LICENSE.model", folder / "LICENSE.stable-diffusion")
    spec = importlib.util.spec_from_file_location("local_pinned_download", ROOT / "scripts/model-download.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    asset = {"name": "instruct-pix2pix-unet.fp16.safetensors", "path": str((folder / "unet.fp16.safetensors").relative_to(ROOT)),
             "url": f"https://huggingface.co/{REPO}/resolve/{REV}/{NAME}?download=true", "bytes": SIZE, "sha256": SHA}
    # Reuse the project's bounded six-worker/32MiB range downloader, including
    # complete ranges from the interrupted single stream. Only this imported
    # module's copy helper changes; shared downloader source remains untouched.
    module.shutil = SimpleNamespace(copyfileobj=lambda incoming, outgoing, length:
                                   shutil.copyfileobj(incoming, outgoing, length=32 * 1024 * 1024))
    destination_unet = ROOT / asset["path"]
    partial = destination_unet.with_suffix(destination_unet.suffix + ".part")
    started = time.monotonic()
    if not destination_unet.exists():
        module.segmented_download(asset, partial)
    candidate = destination_unet if destination_unet.exists() else partial
    actual = digest(candidate)
    if candidate.stat().st_size != SIZE or actual != SHA:
        raise ValueError("Official instruction UNet size/SHA mismatch")
    if candidate == partial:
        partial.replace(destination_unet)
    downloaded = {**asset, "verified_sha256": actual, "elapsed_seconds": round(time.monotonic() - started, 3)}
    original = json.loads((ROOT / "runtime/creation-manifest.json").read_text(encoding="utf-8"))
    source = ROOT / original["model"]["path"]
    assert digest(source) == original["model"]["sha256"]
    destination = folder / "sd15-encoders.safetensors"
    extraction = extract_encoders(source, destination)
    encoders = {"path": str(destination.relative_to(ROOT)), "sha256": digest(destination), "bytes": destination.stat().st_size,
                "source_path": original["model"]["path"], "source_sha256": original["model"]["sha256"],
                "source_revision": original["model"]["revision"], "license": original["model"]["license"]}
    mapping_path = ROOT / "artifacts/creation-instruction-encoder-mapping.json"
    mapping_path.write_text(json.dumps({**extraction, "encoders": encoders}, indent=2), encoding="utf-8")
    manifest = {"schema_version": 1, "profile": "instruct_pix2pix", "backend": original["backend"],
        "model": {"name": REPO, "revision": REV, "path": asset["path"], "sha256": SHA, "bytes": SIZE,
                  "license": "MIT; derived Stable Diffusion restrictions retained", "operation": "instruction_edit"},
        "encoders": encoders, "binaries": original["binaries"], "download": downloaded,
        "notices": [{"path": str(path.relative_to(ROOT)), "sha256": digest(path)} for path in
                    (readme, folder / "LICENSE.instruct-pix2pix", folder / "LICENSE.stable-diffusion")],
        "native_source_revision": "f5df5f8", "encoder_mapping": str(mapping_path.relative_to(ROOT)),
        "default_changed": False, "inference_performed": False}
    (ROOT / "runtime/creation-instruction-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"prepared": True, "unet_bytes": SIZE, "encoders_bytes": encoders["bytes"], "inference": False}), flush=True)


if __name__ == "__main__":
    main()
