#!/usr/bin/env python3
"""
CryptVM Builder — TUI application
Downloads cloud images, builds BIOS-bootable encrypted VM disk images.
Runs directly on Linux/WSL2. Requires root.
"""

import os
import sys
import shutil
import traceback
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll, Center
from textual.screen import Screen
from textual.widgets import (
    Button, Footer, Header, Input, Label, Log,
    ProgressBar, Select, Static, TextArea, Rule,
)

from images import IMAGES


class WelcomeScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Vertical(id="welcome-box"):
                yield Static(
                    "[b]CryptVM Builder v1.0[/b]\n\n"
                    "Build BIOS-bootable VM images with LUKS1 encrypted root.\n"
                    "Supports Debian, Ubuntu, AlmaLinux, and Rocky Linux.\n\n"
                    "Everything runs locally. Passwords never leave your machine.",
                    id="banner",
                )
                yield Static("", id="root-warning")
                yield Button("Start Build", id="btn-start", variant="primary")
        yield Footer()

    def on_mount(self):
        if os.geteuid() != 0:
            self.query_one("#root-warning", Static).update(
                "[red bold]Warning: not running as root. Run with sudo.[/]"
            )

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-start":
            self.app.push_screen(ConfigScreen())


class ConfigScreen(Screen):
    BINDINGS = [Binding("escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="config-scroll"):
            yield Static("[b]Select OS Image[/b]", classes="section-title")
            options = [(info["name"], key) for key, info in IMAGES.items()]
            yield Select(options, id="os-select", prompt="Choose an OS...")

            yield Rule()
            yield Static("[b]LUKS Encryption Password[/b]", classes="section-title")
            yield Static("Unlocks the disk at boot. Use 12+ chars with mixed case/symbols.", classes="hint")
            yield Input(placeholder="LUKS encryption password", password=True, id="luks-password")
            yield Input(placeholder="Confirm LUKS password", password=True, id="luks-password-confirm")

            yield Rule()
            yield Static("[b]Root Password[/b]", classes="section-title")
            yield Input(placeholder="Root user password", password=True, id="root-password")

            yield Rule()
            yield Static("[b]SSH Public Key[/b]", classes="section-title")
            yield Static("Paste your public key (ssh-rsa ..., ssh-ed25519 ..., etc.)", classes="hint")
            yield TextArea(id="ssh-pubkey")

            yield Rule()
            yield Static("[b]Disk Size (MB)[/b]", classes="section-title")
            yield Input(placeholder="10240", value="10240", id="disk-size", type="integer")

            yield Rule()
            yield Static("[b]Output File[/b]", classes="section-title")
            yield Input(placeholder="output.img", value="output.img", id="output-path")

            yield Rule()
            yield Static("", id="validation-msg")
            with Horizontal(id="config-buttons"):
                yield Button("Back", id="btn-back")
                yield Button("Build Image", id="btn-build", variant="primary")
        yield Footer()

    def _validate(self) -> str | None:
        os_select = self.query_one("#os-select", Select)
        if os_select.value == Select.BLANK:
            return "Please select an OS image."

        luks_pw = self.query_one("#luks-password", Input).value
        luks_pw2 = self.query_one("#luks-password-confirm", Input).value
        if len(luks_pw) < 8:
            return "LUKS password must be at least 8 characters."
        if luks_pw != luks_pw2:
            return "LUKS passwords do not match."

        root_pw = self.query_one("#root-password", Input).value
        if not root_pw:
            return "Root password is required."

        ssh_key = self.query_one("#ssh-pubkey", TextArea).text.strip()
        if not ssh_key:
            return "SSH public key is required."
        if not any(ssh_key.startswith(p) for p in ["ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2"]):
            return "SSH key doesn't look valid."

        try:
            size = int(self.query_one("#disk-size", Input).value)
            if size < 2048:
                return "Disk size must be at least 2048 MB."
        except ValueError:
            return "Disk size must be a number."

        if os.geteuid() != 0:
            return "Not running as root. Restart with: sudo python3 cryptvm.py"

        return None

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-build":
            err = self._validate()
            msg = self.query_one("#validation-msg", Static)
            if err:
                msg.update(f"[red bold]{err}[/]")
                return
            msg.update("")

            config = {
                "os_key": self.query_one("#os-select", Select).value,
                "luks_password": self.query_one("#luks-password", Input).value,
                "root_password": self.query_one("#root-password", Input).value,
                "ssh_pubkey": self.query_one("#ssh-pubkey", TextArea).text.strip(),
                "disk_size_mb": int(self.query_one("#disk-size", Input).value),
                "output_path": self.query_one("#output-path", Input).value,
            }
            self.app.push_screen(BuildScreen(config))


class BuildScreen(Screen):
    def __init__(self, config: dict):
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="build-container"):
            yield Static("[b]Building Encrypted VM Image[/b]", id="build-title")
            yield Static("", id="build-status")
            yield ProgressBar(id="build-progress", total=100)
            yield Rule()
            yield Static("[b]Build Log[/b]")
            yield Log(id="build-log", auto_scroll=True, max_lines=5000)
            yield Rule()
            with Horizontal(id="build-buttons"):
                yield Button("Done", id="btn-done", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self):
        self.start_build()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-done":
            self.app.pop_screen()
            self.app.pop_screen()

    @work(thread=True)
    def start_build(self):
        log_widget = self.query_one("#build-log", Log)
        status = self.query_one("#build-status", Static)
        progress = self.query_one("#build-progress", ProgressBar)

        def log(msg):
            self.app.call_from_thread(log_widget.write_line, msg)

        def set_status(msg):
            self.app.call_from_thread(status.update, msg)

        def set_progress(val):
            self.app.call_from_thread(progress.update, progress=val)

        try:
            self._do_build(log, set_status, set_progress)
        except Exception as e:
            log(f"\n[FATAL] {e}")
            log(traceback.format_exc())
            set_status(f"[red bold]Build failed: {e}[/]")

        self.app.call_from_thread(
            self.query_one("#btn-done", Button).__setattr__, "disabled", False
        )

    def _do_build(self, log, set_status, set_progress):
        from downloader import ensure_cloud_image, convert_qcow2_to_raw
        from builder import build_image, check_requirements
        from images import IMAGES

        config = self.config
        os_key = config["os_key"]
        os_info = IMAGES[os_key]

        # Check tools
        set_status("Checking requirements...")
        set_progress(5)
        missing = check_requirements()
        if missing:
            raise FileNotFoundError(f"Missing tools: {', '.join(missing)}\nInstall with: apt install {' '.join(missing)}")
        log("All required tools found.")

        # Download
        set_status(f"Downloading {os_info['name']}...")
        set_progress(10)

        def dl_progress(downloaded, total):
            if total > 0:
                pct = int(10 + (downloaded / total) * 30)
                set_progress(min(pct, 40))

        cloud_img = ensure_cloud_image(os_key, progress_callback=dl_progress)
        log(f"Cloud image: {cloud_img}")

        # Convert qcow2 to raw
        set_status("Converting qcow2 to raw...")
        set_progress(40)
        log("Converting qcow2 to raw...")
        cloud_raw = convert_qcow2_to_raw(cloud_img)
        log(f"Raw image: {cloud_raw}")
        set_progress(50)

        # Build
        output_path = Path(config["output_path"]).resolve()
        set_status("Building image (this takes several minutes)...")

        def build_log(msg):
            log(msg)
            if "LUKS" in msg:
                set_progress(60)
            elif "Extracting" in msg:
                set_progress(70)
            elif "chroot" in msg.lower():
                set_progress(80)
            elif "GRUB" in msg:
                set_progress(85)
            elif "initramfs" in msg:
                set_progress(90)
            elif "complete" in msg.lower():
                set_progress(95)

        success = build_image(
            cloud_image_raw=cloud_raw,
            output_path=output_path,
            disk_size_mb=config["disk_size_mb"],
            luks_password=config["luks_password"],
            root_password=config["root_password"],
            ssh_pubkey=config["ssh_pubkey"],
            os_family=os_info["os_family"],
            log=build_log,
        )

        set_progress(100)
        log("")
        log(f"Image saved to: {output_path}")
        log(f"Size: {output_path.stat().st_size // (1024*1024)} MB")
        log("")
        log("Boot with:")
        log(f"  qemu-system-x86_64 -hda {output_path} -m 1024")
        set_status(f"[green bold]Done! Image: {output_path}[/]")


class CryptVMApp(App):
    CSS = """
    #welcome-box { width: 60; height: auto; margin: 2 0; }
    #banner { text-align: center; margin-bottom: 1; }
    #root-warning { text-align: center; margin-bottom: 1; }
    #btn-start { width: 100%; }
    .section-title { margin-top: 1; }
    .hint { color: $text-muted; }
    #config-scroll { padding: 1 2; }
    #config-buttons { margin-top: 1; height: 3; }
    #config-buttons Button { margin-right: 2; }
    #build-container { padding: 1 2; }
    #build-title { text-align: center; margin-bottom: 1; }
    #build-log { height: 1fr; min-height: 15; border: solid $primary; }
    #build-buttons { margin-top: 1; height: 3; }
    #ssh-pubkey { height: 4; }
    """
    TITLE = "CryptVM Builder"
    BINDINGS = [Binding("q", "quit", "Quit")]

    def on_mount(self):
        self.push_screen(WelcomeScreen())


def main():
    CryptVMApp().run()


if __name__ == "__main__":
    main()
