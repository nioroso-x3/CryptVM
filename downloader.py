"""
Download manager — fetches cloud images with resume support.
"""

import os
import subprocess
import shutil
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError


CACHE_DIR = Path.home() / ".cache" / "cryptvm-builder"


def get_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def download_file(url: str, filename: str, progress_callback=None) -> Path:
    """Download a file with resume support. Returns the local path."""
    cache_dir = get_cache_dir()
    dest = cache_dir / filename
    temp = cache_dir / (filename + ".partial")

    if dest.exists() and dest.stat().st_size > 0:
        if progress_callback:
            sz = dest.stat().st_size
            progress_callback(sz, sz)
        return dest

    existing_size = temp.stat().st_size if temp.exists() else 0
    headers = {}
    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"

    req = Request(url, headers=headers)
    req.add_header("User-Agent", "CryptVM-Builder/1.0")

    try:
        resp = urlopen(req, timeout=30)
    except HTTPError as e:
        if e.code == 416:
            if temp.exists():
                temp.rename(dest)
                return dest
            existing_size = 0
            req = Request(url)
            req.add_header("User-Agent", "CryptVM-Builder/1.0")
            resp = urlopen(req, timeout=30)
        else:
            raise

    content_length = resp.headers.get("Content-Length")
    total = int(content_length) + existing_size if content_length else 0

    mode = "ab" if existing_size > 0 and resp.status == 206 else "wb"
    if mode == "wb":
        existing_size = 0

    downloaded = existing_size

    with open(temp, mode) as f:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback:
                progress_callback(downloaded, total)

    temp.rename(dest)
    return dest


def ensure_cloud_image(image_key: str, progress_callback=None) -> Path:
    from images import IMAGES
    info = IMAGES[image_key]
    return download_file(info["url"], info["filename"], progress_callback)


def convert_qcow2_to_raw(qcow2_path: Path, progress_callback=None) -> Path:
    raw_path = qcow2_path.with_suffix(".raw")
    if raw_path.exists():
        if progress_callback:
            sz = raw_path.stat().st_size
            progress_callback(sz, sz)
        return raw_path

    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise FileNotFoundError("qemu-img not found. Install qemu-utils.")

    if progress_callback:
        progress_callback(0, 0)

    subprocess.run(
        [qemu_img, "convert", "-f", "qcow2", "-O", "raw", str(qcow2_path), str(raw_path)],
        check=True,
    )

    if progress_callback:
        sz = raw_path.stat().st_size
        progress_callback(sz, sz)

    return raw_path
