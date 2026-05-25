"""
Builder — runs directly on Linux/WSL2.
Requires root. Uses losetup, cryptsetup, parted, chroot, grub-install.
"""

import os
import subprocess
import shutil
import textwrap
from pathlib import Path


def check_requirements(boot_mode: str = "bios") -> list[str]:
    """Check for required tools. Returns list of missing ones."""
    required = ["losetup", "cryptsetup", "parted", "mkfs.ext4", "mount", "chroot", "tar", "blkid"]

    if boot_mode == "uefi":
        required.extend(["mkfs.fat", "efibootmgr"])

    missing = [cmd for cmd in required if not shutil.which(cmd)]
    return missing


def check_root() -> bool:
    return os.geteuid() == 0


def run(cmd, **kwargs):
    """Run a command, raising on failure with output included."""
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result


def build_image(
    cloud_image_raw: Path,
    output_path: Path,
    disk_size_mb: int,
    luks_password: str,
    root_password: str,
    ssh_pubkey: str,
    os_family: str,
    boot_mode: str = "bios",
    os_name: str = "Linux",
    enable_cloud_init: bool = False,
    enable_serial: bool = False,
    log=None,
):
    """
    Build a BIOS or UEFI-bootable encrypted disk image.

    cloud_image_raw: path to the raw cloud image
    output_path: where to write the final .img
    disk_size_mb: total disk size in MB
    luks_password: LUKS encryption passphrase
    root_password: root user password
    ssh_pubkey: SSH public key for root
    os_family: "debian" or "redhat"
    boot_mode: "bios" or "uefi"
    enable_cloud_init: enable cloud-init service (default: False)
    log: callable for status messages
    """
    if log is None:
        log = print

    if not check_root():
        raise PermissionError("Must run as root (use sudo).")

    missing = check_requirements(boot_mode)
    if missing:
        raise FileNotFoundError(f"Missing required tools: {', '.join(missing)}")

    loop_dev = None
    loop_cloud = None
    luks_open = False
    mounts = []

    try:
        # ── Extract rootfs from cloud image ──────────────────────────
        log("Mounting cloud image to extract rootfs...")
        loop_cloud = run(["losetup", "--find", "--show", "--partscan", str(cloud_image_raw)]).stdout.strip()
        log(f"  Cloud image loop: {loop_cloud}")

        subprocess.run(["partprobe", loop_cloud], capture_output=True)
        subprocess.run(["sleep", "1"])

        # Find root partition (largest ext4/xfs)
        cloud_root = _find_root_partition(loop_cloud)
        log(f"  Cloud root partition: {cloud_root}")

        cloud_mnt = Path("/tmp/cryptvm-cloud-root")
        cloud_mnt.mkdir(exist_ok=True)
        run(["mount", "-o", "ro", cloud_root, str(cloud_mnt)])
        mounts.append(str(cloud_mnt))

        log("Creating rootfs tarball...")
        tarball = Path("/tmp/cryptvm-rootfs.tar")
        run(["tar", "-C", str(cloud_mnt), "-cpf", str(tarball), "."])
        tarball_mb = tarball.stat().st_size // (1024 * 1024)
        log(f"  Rootfs tarball: {tarball_mb}MB")

        run(["umount", str(cloud_mnt)])
        mounts.remove(str(cloud_mnt))
        run(["losetup", "-d", loop_cloud])
        loop_cloud = None

        # ── Create output disk ───────────────────────────────────────
        log(f"Creating output disk ({disk_size_mb}MB)...")
        run(["dd", "if=/dev/zero", f"of={output_path}", "bs=1M", "count=1", "seek=" + str(disk_size_mb - 1)])

        if boot_mode == "uefi":
            log("Creating GPT partition table for UEFI...")
            run(["parted", "-s", str(output_path), "mklabel", "gpt"])
            # EFI System Partition (512MB)
            run(["parted", "-s", str(output_path), "mkpart", "primary", "fat32", "1MiB", "513MiB"])
            run(["parted", "-s", str(output_path), "set", "1", "esp", "on"])
            # Boot partition (512MB)
            run(["parted", "-s", str(output_path), "mkpart", "primary", "ext4", "513MiB", "1025MiB"])
            # Root partition (rest)
            run(["parted", "-s", str(output_path), "mkpart", "primary", "ext4", "1025MiB", "100%"])
        else:
            log("Creating MBR partition table for BIOS...")
            boot_end = 513  # 512MB boot + 1MB alignment
            run(["parted", "-s", str(output_path), "mklabel", "msdos"])
            run(["parted", "-s", str(output_path), "mkpart", "primary", "ext4", "1MiB", f"{boot_end}MiB"])
            run(["parted", "-s", str(output_path), "mkpart", "primary", "ext4", f"{boot_end}MiB", "100%"])
            run(["parted", "-s", str(output_path), "set", "1", "boot", "on"])

        # Set up loop device with partitions
        loop_dev = run(["losetup", "--find", "--show", "--partscan", str(output_path)]).stdout.strip()
        subprocess.run(["partprobe", loop_dev], capture_output=True)
        subprocess.run(["sleep", "1"])

        if boot_mode == "uefi":
            efi_dev = f"{loop_dev}p1"
            boot_dev = f"{loop_dev}p2"
            root_dev = f"{loop_dev}p3"

            if not os.path.exists(boot_dev):
                raise RuntimeError(f"Boot partition {boot_dev} not found. Loop device partitions not created.")

            log(f"  Loop: {loop_dev}, EFI: {efi_dev}, boot: {boot_dev}, root: {root_dev}")

            # ── Format EFI System Partition ──────────────────────────────
            log("Formatting EFI System Partition...")
            run(["mkfs.fat", "-F", "32", "-n", "EFI", efi_dev])

            # ── Format boot ──────────────────────────────────────────────
            log("Formatting /boot...")
            run(["mkfs.ext4", "-L", "boot", boot_dev])
        else:
            boot_dev = f"{loop_dev}p1"
            root_dev = f"{loop_dev}p2"

            if not os.path.exists(boot_dev):
                raise RuntimeError(f"Boot partition {boot_dev} not found. Loop device partitions not created.")

            log(f"  Loop: {loop_dev}, boot: {boot_dev}, root: {root_dev}")

            # ── Format boot ──────────────────────────────────────────────
            log("Formatting /boot...")
            run(["mkfs.ext4", "-L", "boot", boot_dev])

        # ── LUKS setup ───────────────────────────────────────────────
        log("Setting up LUKS1 encryption...")
        run(
            ["cryptsetup", "luksFormat", "--type", "luks1",
             "--cipher", "aes-xts-plain64", "--key-size", "512",
             "--hash", "sha256", "--iter-time", "2000",
             "--batch-mode", root_dev],
            input=luks_password,
        )

        run(["cryptsetup", "luksOpen", root_dev, "cryptroot"], input=luks_password)
        luks_open = True

        log("Formatting encrypted root...")
        run(["mkfs.ext4", "-L", "root", "/dev/mapper/cryptroot"])

        # ── Mount and populate ───────────────────────────────────────
        target = Path("/tmp/cryptvm-target")
        target.mkdir(exist_ok=True)
        run(["mount", "/dev/mapper/cryptroot", str(target)])
        mounts.append(str(target))

        log("Extracting rootfs to encrypted volume...")
        run(["tar", "-C", str(target), "-xpf", str(tarball)])
        log("  Rootfs extracted.")

        # The cloud image has kernel/initrd in its /boot directory on the
        # root filesystem. We need to move those files to our separate /boot
        # partition. First, collect them before mounting over the directory.
        boot_files = list((target / "boot").glob("*"))
        log(f"  Found {len(boot_files)} files in rootfs /boot/")

        # Mount EFI partition first for UEFI systems
        if boot_mode == "uefi":
            efi_dir = target / "boot" / "efi"
            efi_dir.mkdir(parents=True, exist_ok=True)

        # Now mount the real /boot partition on top
        run(["mount", boot_dev, str(target / "boot")])
        mounts.append(str(target / "boot"))

        # For UEFI, also mount the EFI System Partition
        if boot_mode == "uefi":
            efi_dir = target / "boot" / "efi"
            efi_dir.mkdir(parents=True, exist_ok=True)
            run(["mount", efi_dev, str(target / "boot" / "efi")])
            mounts.append(str(target / "boot" / "efi"))

        # Move kernel/initrd/config/map files from root's boot into the partition
        # They were extracted to the encrypted root but are now hidden by the mount.
        # We need to read them from the underlying filesystem.
        # Unmount /boot briefly, copy files, remount.
        if boot_mode == "uefi":
            run(["umount", str(target / "boot" / "efi")])
            mounts.remove(str(target / "boot" / "efi"))
        run(["umount", str(target / "boot")])
        mounts.remove(str(target / "boot"))

        # Now /boot shows the files from the root filesystem
        boot_contents = list((target / "boot").iterdir())
        log(f"  Boot files to copy: {[f.name for f in boot_contents if not f.is_dir() or f.name != 'lost+found']}")

        # Create a temp copy
        import tempfile
        boot_tmp = Path(tempfile.mkdtemp(prefix="cryptvm-boot-"))
        for item in boot_contents:
            if item.name == "lost+found":
                continue
            if item.is_dir():
                shutil.copytree(item, boot_tmp / item.name, symlinks=True)
            else:
                shutil.copy2(item, boot_tmp / item.name)

        # Remount the boot partition and copy files in
        run(["mount", boot_dev, str(target / "boot")])
        mounts.append(str(target / "boot"))

        # Remount EFI partition for UEFI
        if boot_mode == "uefi":
            efi_dir = target / "boot" / "efi"
            efi_dir.mkdir(parents=True, exist_ok=True)
            run(["mount", efi_dev, str(target / "boot" / "efi")])
            mounts.append(str(target / "boot" / "efi"))

        for item in boot_tmp.iterdir():
            dest = target / "boot" / item.name
            if item.is_dir():
                shutil.copytree(item, dest, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        shutil.rmtree(boot_tmp)

        # Verify
        kernels = list((target / "boot").glob("vmlinuz-*"))
        initrds = list((target / "boot").glob("initrd*")) + list((target / "boot").glob("initramfs*"))
        log(f"  Kernels on /boot partition: {[k.name for k in kernels]}")
        log(f"  Initrds on /boot partition: {[i.name for i in initrds]}")
        if not kernels:
            log("  WARNING: No kernel found in /boot! The image may not boot.")

        # ── Get UUIDs ────────────────────────────────────────────────
        boot_uuid = run(["blkid", "-s", "UUID", "-o", "value", boot_dev]).stdout.strip()
        luks_uuid = run(["blkid", "-s", "UUID", "-o", "value", root_dev]).stdout.strip()
        root_uuid = run(["blkid", "-s", "UUID", "-o", "value", "/dev/mapper/cryptroot"]).stdout.strip()
        log(f"  Boot UUID: {boot_uuid}")
        log(f"  LUKS UUID: {luks_uuid}")
        log(f"  Root UUID: {root_uuid}")

        efi_uuid = None
        if boot_mode == "uefi":
            efi_uuid = run(["blkid", "-s", "UUID", "-o", "value", efi_dev]).stdout.strip()
            log(f"  EFI UUID: {efi_uuid}")

        # ── Configure system ─────────────────────────────────────────
        log("Configuring fstab and crypttab...")
        fstab_content = (
            f"UUID={root_uuid}  /      ext4  errors=remount-ro  0  1\n"
            f"UUID={boot_uuid}  /boot  ext4  defaults           0  2\n"
        )

        if boot_mode == "uefi":
            fstab_content += f"UUID={efi_uuid}  /boot/efi  vfat  defaults,umask=0077  0  2\n"

        (target / "etc/fstab").write_text(fstab_content)
        (target / "etc/crypttab").write_text(
            f"cryptroot UUID={luks_uuid} none luks\n"
        )

        # ── Root password ────────────────────────────────────────────
        log("Setting root password...")
        _set_root_password(target, root_password)

        # ── SELinux - configure for proper first boot
        if os_family == "redhat":
            log("Configuring SELinux for first boot...")
            selinux_config = target / "etc/selinux/config"
            if selinux_config.exists():
                content = selinux_config.read_text()
                import re
                content = re.sub(r'^SELINUX=enforcing', 'SELINUX=permissive', content, flags=re.MULTILINE)
                content = re.sub(r'^SELINUX=disabled', 'SELINUX=permissive', content, flags=re.MULTILINE)
                selinux_config.write_text(content)
                log("  Set SELinux to permissive mode for first boot")

                (target / ".autorelabel").touch()
                log("  Created .autorelabel for SELinux context restoration on first boot")
                log("  Note: SELinux will remain in permissive mode - manually set to enforcing after first boot if desired")
        else:
            log("Skipping SELinux configuration for non-RHEL OS")

        # ── SSH ──────────────────────────────────────────────────────
        log("Configuring SSH...")
        result = subprocess.run(
            ["chroot", str(target), "ssh-keygen", "-A"],
            capture_output=True, text=True,
        )
        ssh_dir = target / "root/.ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        (ssh_dir / "authorized_keys").write_text(ssh_pubkey + "\n")
        os.chmod(ssh_dir / "authorized_keys", 0o600)

        sshd_conf = target / "etc/ssh/sshd_config"
        if sshd_conf.exists():
            text = sshd_conf.read_text()
            import re
            text = re.sub(r'^#?PermitRootLogin.*', 'PermitRootLogin prohibit-password', text, flags=re.MULTILINE)
            text = re.sub(r'^#?PubkeyAuthentication.*', 'PubkeyAuthentication yes', text, flags=re.MULTILINE)
            text = re.sub(r'^#?PasswordAuthentication.*', 'PasswordAuthentication no', text, flags=re.MULTILINE)
            sshd_conf.write_text(text)

        sshd_drop = target / "etc/ssh/sshd_config.d"
        if sshd_drop.is_dir():
            (sshd_drop / "99-cryptvm.conf").write_text(
                "PermitRootLogin prohibit-password\n"
                "PubkeyAuthentication yes\n"
                "PasswordAuthentication no\n"
            )

        # Enable sshd
        systemd_wants = target / "etc/systemd/system/multi-user.target.wants"
        systemd_wants.mkdir(parents=True, exist_ok=True)
        for svc in ["ssh.service", "sshd.service"]:
            svc_path = target / f"lib/systemd/system/{svc}"
            if svc_path.exists():
                link = systemd_wants / svc
                link.unlink(missing_ok=True)
                link.symlink_to(f"/lib/systemd/system/{svc}")

        # ── Configure cloud-init ─────────────────────────────────────
        if enable_cloud_init:
            log("Enabling cloud-init...")
            cloud_dir = target / "etc/cloud"
            cloud_dir.mkdir(parents=True, exist_ok=True)

            disabled_file = cloud_dir / "cloud-init.disabled"
            disabled_file.unlink(missing_ok=True)

            for svc in ["cloud-init.service", "cloud-init-local.service",
                       "cloud-config.service", "cloud-final.service"]:
                link = target / f"etc/systemd/system/{svc}"
                link.unlink(missing_ok=True)

            systemd_wants_multi = target / "etc/systemd/system/multi-user.target.wants"
            systemd_wants_multi.mkdir(parents=True, exist_ok=True)
            systemd_wants_cloud = target / "etc/systemd/system/cloud-init.target.wants"
            systemd_wants_cloud.mkdir(parents=True, exist_ok=True)

            for svc in ["cloud-init.service", "cloud-config.service", "cloud-final.service"]:
                svc_path = target / f"lib/systemd/system/{svc}"
                if svc_path.exists():
                    link = systemd_wants_multi / svc
                    link.unlink(missing_ok=True)
                    link.symlink_to(f"/lib/systemd/system/{svc}")

            svc_local = "cloud-init-local.service"
            svc_local_path = target / f"lib/systemd/system/{svc_local}"
            if svc_local_path.exists():
                link = target / f"etc/systemd/system/sysinit.target.wants/{svc_local}"
                link.parent.mkdir(parents=True, exist_ok=True)
                link.unlink(missing_ok=True)
                link.symlink_to(f"/lib/systemd/system/{svc_local}")
        else:
            log("Disabling cloud-init...")
            (target / "etc/cloud").mkdir(parents=True, exist_ok=True)
            (target / "etc/cloud/cloud-init.disabled").touch()
            for svc in ["cloud-init.service", "cloud-init-local.service",
                         "cloud-config.service", "cloud-final.service"]:
                link = target / f"etc/systemd/system/{svc}"
                link.unlink(missing_ok=True)
                link.symlink_to("/dev/null")

        # ── Serial console configuration ─────────────────────────────
        if not enable_serial:
            log("Disabling serial console configuration...")
            for service in ["serial-getty@ttyS0.service", "getty@ttyS0.service"]:
                link = target / f"etc/systemd/system/getty.target.wants/{service}"
                link.unlink(missing_ok=True)
                link = target / f"etc/systemd/system/{service}"
                link.unlink(missing_ok=True)
                link.symlink_to("/dev/null")
            log("  Disabled serial getty services")
        else:
            log("Keeping serial console enabled")

        # ── Hostname / networking ────────────────────────────────────
        (target / "etc/hostname").write_text("cryptvm\n")
        (target / "etc/hosts").write_text(
            "127.0.0.1  localhost\n127.0.1.1  cryptvm\n"
            "::1        localhost ip6-localhost ip6-loopback\n"
        )

        # systemd-networkd DHCP
        netdir = target / "etc/systemd/network"
        netdir.mkdir(parents=True, exist_ok=True)
        (netdir / "20-wired.network").write_text(
            "[Match]\nName=en* eth*\n\n[Network]\nDHCP=yes\n"
        )

        # /etc/network/interfaces for Debian
        if (target / "etc/network").is_dir():
            (target / "etc/network/interfaces").write_text(
                "auto lo\niface lo inet loopback\n\n"
                "auto eth0\niface eth0 inet dhcp\n\n"
                "allow-hotplug ens3\niface ens3 inet dhcp\n\n"
                "allow-hotplug enp0s3\niface enp0s3 inet dhcp\n"
            )

        # ── Chroot: install packages, GRUB, initramfs ────────────────
        log("Setting up chroot for GRUB + initramfs...")
        _bind_mount(target, mounts, loop_dev)

        # Copy host's DNS config so apt/dnf can resolve inside chroot
        resolv_target = target / "etc/resolv.conf"
        resolv_target.unlink(missing_ok=True)  # might be a symlink to systemd-resolved
        try:
            resolv_target.write_text(Path("/etc/resolv.conf").read_text())
        except Exception:
            resolv_target.write_text("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")

        chroot_script = _make_chroot_script(luks_uuid, os_family, loop_dev, boot_mode, os_name, enable_serial)
        script_path = target / "tmp/setup-grub.sh"
        script_path.write_text(chroot_script)
        script_path.chmod(0o755)

        log("Running chroot setup (this may take a few minutes)...")
        result = subprocess.run(
            ["chroot", str(target), "/bin/bash", "/tmp/setup-grub.sh"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            log(f"  [chroot] {line}")
        if result.returncode != 0:
            for line in result.stderr.splitlines():
                log(f"  [chroot:err] {line}")
            log(f"  WARNING: chroot exited with code {result.returncode}")

        script_path.unlink(missing_ok=True)

        log("Build complete!")
        return True

    finally:
        # ── Cleanup in reverse order ─────────────────────────────────
        for m in reversed(mounts):
            subprocess.run(["umount", "-lf", m], capture_output=True)
        if luks_open:
            subprocess.run(["cryptsetup", "luksClose", "cryptroot"], capture_output=True)
        if loop_dev:
            subprocess.run(["losetup", "-d", loop_dev], capture_output=True)
        if loop_cloud:
            subprocess.run(["losetup", "-d", loop_cloud], capture_output=True)
        # Clean temp files
        for p in [Path("/tmp/cryptvm-rootfs.tar")]:
            p.unlink(missing_ok=True)
        for d in [Path("/tmp/cryptvm-cloud-root"), Path("/tmp/cryptvm-target")]:
            if d.exists():
                subprocess.run(["rm", "-rf", str(d)], capture_output=True)


def _find_root_partition(loop_dev: str) -> str:
    """Find the root filesystem partition in a cloud image."""
    best = None
    best_size = 0

    # Check partitions
    for suffix in ["p1", "p2", "p3","p4", "1", "2", "3","4"]:
        dev = f"{loop_dev}{suffix}"
        if not os.path.exists(dev):
            continue
        try:
            fstype = subprocess.run(
                ["blkid", "-s", "TYPE", "-o", "value", dev],
                capture_output=True, text=True
            ).stdout.strip()
            if fstype in ("ext4", "ext3", "xfs"):
                size = int(subprocess.run(
                    ["blockdev", "--getsize64", dev],
                    capture_output=True, text=True
                ).stdout.strip())
                if size > best_size:
                    best = dev
                    best_size = size
        except Exception:
            continue

    # Maybe the whole device is a filesystem
    if not best:
        fstype = subprocess.run(
            ["blkid", "-s", "TYPE", "-o", "value", loop_dev],
            capture_output=True, text=True
        ).stdout.strip()
        if fstype in ("ext4", "ext3", "xfs"):
            best = loop_dev

    if not best:
        raise RuntimeError(f"Could not find root filesystem in cloud image on {loop_dev}")
    return best


def _set_root_password(target: Path, password: str):
    """Set root password using chpasswd or openssl."""
    result = subprocess.run(
        ["chroot", str(target), "chpasswd"],
        input=f"root:{password}\n",
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Fallback: use openssl to generate hash and patch /etc/shadow
        hash_result = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            input=password,
            capture_output=True, text=True,
        )
        if hash_result.returncode == 0:
            pw_hash = hash_result.stdout.strip()
            shadow = target / "etc/shadow"
            if shadow.exists():
                import re
                text = shadow.read_text()
                text = re.sub(r'^root:[^:]*:', f'root:{pw_hash}:', text, flags=re.MULTILINE)
                shadow.write_text(text)

def _bind_mount(target: Path, mounts: list, loop_dev: str):
    """Bind-mount /dev, /proc, /sys, /run into the chroot.

    We bind-mount the host's /dev because apt, dpkg, depmod, etc. need
    working /dev/null, /dev/urandom, etc. This does expose host block
    devices inside the chroot, but since we write grub.cfg manually
    (not via grub-mkconfig), that doesn't matter.
    """
    for d in ["dev", "dev/pts"]:
        src = f"/{d}"
        dst = str(target / d)
        Path(dst).mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "--bind", src, dst], check=True, capture_output=True)
        mounts.append(dst)

    for d, fstype in [("proc", "proc"), ("sys", "sysfs"), ("run", "tmpfs")]:
        dst = str(target / d)
        Path(dst).mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "-t", fstype, fstype, dst], check=True, capture_output=True)
        mounts.append(dst)


def _make_chroot_script(luks_uuid: str, os_family: str, loop_dev: str, boot_mode: str = "bios", os_name: str = "Linux", enable_serial: bool = False) -> str:
    """Generate the shell script that runs inside chroot."""
    return textwrap.dedent(f"""\
        #!/bin/bash
        set -e
        export DEBIAN_FRONTEND=noninteractive
        export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"

        # Ensure DNS works inside chroot
        if [ ! -s /etc/resolv.conf ]; then
            echo "nameserver 8.8.8.8" > /etc/resolv.conf
            echo "nameserver 1.1.1.1" >> /etc/resolv.conf
        fi

        echo "=== Installing packages ==="
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq || true

            # Install a bootable kernel — cloud images often use linux-kvm
            # which may not have boot files in /boot. Install linux-generic
            # to get a full kernel with /boot/vmlinuz-* and initrd.
            if ! ls /boot/vmlinuz-* >/dev/null 2>&1; then
                echo "No kernel in /boot, installing linux-generic..."
                apt-get install -y -qq linux-generic 2>&1 || \\
                apt-get install -y -qq linux-image-generic 2>&1 || true
            fi

            # Force reinstall the kernel image to ensure vmlinuz is in /boot
            # (it may already be "installed" but vmlinuz missing from our /boot partition)
            if ! ls /boot/vmlinuz-* >/dev/null 2>&1; then
                echo "vmlinuz still missing, force reinstalling kernel image..."
                KPKG=$(dpkg -l | grep linux-image-[0-9] | awk '{{print $2}}' | head -1)
                if [ -n "$KPKG" ]; then
                    apt-get install -y --reinstall "$KPKG" 2>&1 || true
                fi
            fi

            # Last resort: extract vmlinuz from the .deb directly
            if ! ls /boot/vmlinuz-* >/dev/null 2>&1; then
                echo "vmlinuz STILL missing, searching for it..."
                # It might be at /boot/vmlinuz on the root fs (not -versioned)
                [ -f /boot/vmlinuz ] && echo "Found /boot/vmlinuz (unversioned)"
                # Check if dpkg knows where it put it
                KPKG=$(dpkg -l | grep linux-image-[0-9] | awk '{{print $2}}' | head -1)
                if [ -n "$KPKG" ]; then
                    dpkg -L "$KPKG" | grep vmlinuz || true
                fi
            fi

            if [ "{boot_mode}" = "uefi" ]; then
                # Remove cloud/BIOS GRUB variants that conflict with UEFI GRUB.
                # grub-cloud-amd64 is Debian's cloud-specific package that assumes
                # BIOS boot — its postinst runs grub-install --target=i386-pc which
                # fails on GPT/UEFI disks with no BIOS Boot Partition.
                echo "Removing conflicting GRUB packages..."
                apt-get remove -y --purge grub-cloud-amd64 grub-pc grub-pc-bin 2>&1 || true
                apt-get install -y -qq cryptsetup cryptsetup-initramfs grub-efi-amd64 grub-efi-amd64-bin \
                    grub-efi-amd64-signed shim-signed efibootmgr \
                    openssh-server 2>&1 || true
            else
                # For BIOS mode, remove UEFI GRUB if present
                echo "Removing conflicting GRUB packages..."
                apt-get remove -y --purge grub-cloud-amd64 grub-efi-amd64 grub-efi-amd64-bin 2>&1 || true
                apt-get install -y -qq cryptsetup cryptsetup-initramfs grub-pc \\
                    openssh-server 2>&1 || true
            fi
            # Ensure cryptsetup is always included in initramfs for ALL kernels,
            # including HWE kernels installed later. This needs multiple hooks:
            mkdir -p /etc/cryptsetup-initramfs
            echo "CRYPTSETUP=y" > /etc/cryptsetup-initramfs/conf-hook

            # Also set ASKPASS=y to ensure the password prompt is included
            if ! grep -q "^ASKPASS=y" /etc/cryptsetup-initramfs/conf-hook 2>/dev/null; then
                echo "ASKPASS=y" >> /etc/cryptsetup-initramfs/conf-hook
            fi

            # Create an initramfs-tools hook to force cryptsetup inclusion.
            # This survives kernel changes because initramfs-tools runs all
            # hooks in /etc/initramfs-tools/hooks/ for every kernel.
            mkdir -p /etc/initramfs-tools/hooks
            cat > /etc/initramfs-tools/hooks/cryptvm-force-cryptsetup << 'HOOKEOF'
#!/bin/sh
PREREQ="cryptroot"
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
# Force copy cryptsetup and related binaries
copy_exec /sbin/cryptsetup /sbin
copy_exec /sbin/dmsetup /sbin
exit 0
HOOKEOF
            chmod +x /etc/initramfs-tools/hooks/cryptvm-force-cryptsetup

            # Ensure the initramfs-tools conf includes the cryptroot script
            mkdir -p /etc/initramfs-tools/conf.d
            echo "CRYPTROOT=target=cryptroot,source=UUID=$(blkid -s UUID -o value /dev/mapper/cryptroot 2>/dev/null || echo UNKNOWN)" \
                > /etc/initramfs-tools/conf.d/cryptvm.conf 2>/dev/null || true

            # If dracut is also installed (some Ubuntu variants), configure it too
            if command -v dracut >/dev/null 2>&1; then
                mkdir -p /etc/dracut.conf.d
                cat > /etc/dracut.conf.d/99-cryptvm.conf << 'DRACUTCONF'
add_dracutmodules+=" crypt dm rootfs-block "
install_items+=" /etc/crypttab /usr/sbin/cryptsetup "
DRACUTCONF
            fi

        elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
            PKG_MGR="dnf"
            command -v dnf >/dev/null 2>&1 || PKG_MGR="yum"

            if ! ls /boot/vmlinuz-* >/dev/null 2>&1; then
                echo "No kernel in /boot, installing kernel..."
                $PKG_MGR install -y kernel 2>&1 || true
            fi

            if [ "{boot_mode}" = "uefi" ]; then
                $PKG_MGR install -y -q cryptsetup grub2-efi-x64 grub2-efi-x64-modules \
                    shim-x64 efibootmgr openssh-server 2>&1 || true
            else
                $PKG_MGR install -y -q cryptsetup grub2 grub2-pc grub2-pc-modules \\
                    openssh-server 2>&1 || true
            fi

            # ── RHEL: Configure /etc/default/grub with rd.luks.* params ──
            # This is critical for surviving kernel updates. When dnf installs
            # a new kernel, it runs grub2-mkconfig which reads /etc/default/grub.
            # Without rd.luks.uuid in GRUB_CMDLINE_LINUX, the new grub.cfg
            # won't have LUKS parameters and the system won't boot.
            echo "Configuring /etc/default/grub for LUKS..."
            cat > /etc/default/grub << 'GRUB_RHEL_STATIC'
GRUB_TIMEOUT=5
GRUB_DISTRIBUTOR="$(sed 's, release .*$,,g' /etc/system-release)"
GRUB_DEFAULT=saved
GRUB_DISABLE_SUBMENU=true
GRUB_DISABLE_OS_PROBER=true
GRUB_DISABLE_RECOVERY=true
GRUB_RHEL_STATIC
            {"" if enable_serial else "echo 'GRUB_TERMINAL_OUTPUT=\"console\"' >> /etc/default/grub"}
            {"echo 'GRUB_TERMINAL_OUTPUT=\"serial console\"' >> /etc/default/grub" if enable_serial else ""}
            {"echo 'GRUB_SERIAL_COMMAND=\"serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1\"' >> /etc/default/grub" if enable_serial else ""}
            # Append the LUKS-specific kernel cmdline with the actual UUID
            echo 'GRUB_CMDLINE_LINUX="rd.luks.uuid={luks_uuid} rd.luks.name={luks_uuid}=cryptroot root=/dev/mapper/cryptroot{"" if not enable_serial else " console=ttyS0,115200 console=tty0"}"' >> /etc/default/grub
            echo 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"' >> /etc/default/grub
            echo "Written /etc/default/grub:"
            cat /etc/default/grub
        fi

        echo "=== Checking /boot contents ==="
        ls -la /boot/

        if [ "{boot_mode}" = "uefi" ]; then
            echo "=== Installing GRUB for UEFI ==="
            if command -v grub-install >/dev/null 2>&1; then
                grub-install --target=x86_64-efi --efi-directory=/boot/efi --no-nvram 2>&1 || true
            elif command -v grub2-install >/dev/null 2>&1; then
                export GRUB_DISABLE_SUBMENU=true
                grub2-install --target=x86_64-efi --efi-directory=/boot/efi --no-nvram --force 2>&1 || true
            fi

            echo "=== Setting up UEFI boot chain (shim + GRUB) ==="
            mkdir -p /boot/efi/EFI/BOOT

            # Detect the distro-specific EFI directory
            EFI_DISTRO_DIR=""
            for d in /boot/efi/EFI/*/; do
                dname=$(basename "$d")
                case "$dname" in
                    BOOT|boot) continue ;;
                    *) EFI_DISTRO_DIR="$d"; break ;;
                esac
            done

            if [ -z "$EFI_DISTRO_DIR" ]; then
                # Create one based on OS family
                if [ "{os_family}" = "debian" ]; then
                    EFI_DISTRO_DIR="/boot/efi/EFI/ubuntu"
                else
                    EFI_DISTRO_DIR="/boot/efi/EFI/almalinux"
                fi
                mkdir -p "$EFI_DISTRO_DIR"
            fi
            echo "EFI distro directory: $EFI_DISTRO_DIR"

            # Find shimx64.efi — this is the Secure Boot signed first-stage loader
            SHIM=""
            for candidate in \\
                "$EFI_DISTRO_DIR/shimx64.efi" \\
                /boot/efi/EFI/*/shimx64.efi \\
                /usr/lib/shim/shimx64.efi \\
                /usr/lib/shim/shimx64.efi.signed \\
                /usr/share/shim-signed/shimx64.efi.signed \\
                /usr/share/shim/shimx64.efi; do
                if [ -f "$candidate" ]; then
                    SHIM="$candidate"
                    break
                fi
            done

            # Find grubx64.efi
            GRUB_EFI=""
            for candidate in \\
                "$EFI_DISTRO_DIR/grubx64.efi" \\
                /boot/efi/EFI/*/grubx64.efi \\
                /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed \\
                /usr/share/grub/x86_64-efi-signed/grubx64.efi.signed; do
                if [ -f "$candidate" ]; then
                    GRUB_EFI="$candidate"
                    break
                fi
            done

            echo "Found shim: $SHIM"
            echo "Found GRUB EFI: $GRUB_EFI"

            if [ -n "$SHIM" ]; then
                # Copy shim to the distro dir and fallback
                cp "$SHIM" "$EFI_DISTRO_DIR/shimx64.efi" 2>/dev/null || true
                cp "$SHIM" /boot/efi/EFI/BOOT/BOOTX64.EFI
                echo "Installed shimx64.efi as BOOTX64.EFI (Secure Boot chain)"

                # Copy grubx64.efi next to the shim (shim loads grubx64.efi from same dir)
                if [ -n "$GRUB_EFI" ]; then
                    cp "$GRUB_EFI" "$EFI_DISTRO_DIR/grubx64.efi" 2>/dev/null || true
                    cp "$GRUB_EFI" /boot/efi/EFI/BOOT/grubx64.efi
                    echo "Installed grubx64.efi alongside shim"
                fi

                # Also copy mmx64.efi (MOK manager) if available
                for mok in "$EFI_DISTRO_DIR/mmx64.efi" /usr/lib/shim/mmx64.efi /usr/share/shim-signed/mmx64.efi.signed; do
                    if [ -f "$mok" ]; then
                        cp "$mok" "$EFI_DISTRO_DIR/mmx64.efi" 2>/dev/null || true
                        cp "$mok" /boot/efi/EFI/BOOT/mmx64.efi
                        echo "Installed mmx64.efi (MOK manager)"
                        break
                    fi
                done
            elif [ -n "$GRUB_EFI" ]; then
                # No shim available — fall back to unsigned GRUB as BOOTX64.EFI
                # This works with OVMF without Secure Boot or with it disabled
                cp "$GRUB_EFI" /boot/efi/EFI/BOOT/BOOTX64.EFI
                echo "WARNING: No shim found. Installed unsigned grubx64.efi as BOOTX64.EFI"
                echo "  This will NOT work with Secure Boot enabled."
                echo "  Use OVMF without Secure Boot or install shim-x64/shim-signed."
            else
                echo "ERROR: Neither shim nor GRUB EFI binary found!"
                echo "  Checking what's on the ESP:"
                find /boot/efi -type f -name "*.efi" 2>/dev/null || true
                echo "  Checking installed packages:"
                rpm -qa | grep -i -E "shim|grub.*efi" 2>/dev/null || dpkg -l | grep -i -E "shim|grub.*efi" 2>/dev/null || true
            fi

            echo "Final EFI layout:"
            find /boot/efi -type f 2>/dev/null | sort
        else
            echo "=== Installing GRUB to MBR ==="
            if command -v grub-install >/dev/null 2>&1; then
                grub-install --target=i386-pc --boot-directory=/boot "{loop_dev}" 2>&1 || true
            elif command -v grub2-install >/dev/null 2>&1; then
                grub2-install --target=i386-pc --boot-directory=/boot "{loop_dev}" 2>&1 || true
            fi
        fi

        echo "=== Rebuilding initramfs with cryptsetup ==="
        if command -v update-initramfs >/dev/null 2>&1; then
            update-initramfs -u -k all 2>&1 || true
        elif command -v dracut >/dev/null 2>&1; then
            # Configure dracut to always include LUKS support in every initramfs.
            # This is critical: without it, kernel updates via dnf produce
            # initramfs images that can't unlock the encrypted root.
            mkdir -p /etc/dracut.conf.d
            cat > /etc/dracut.conf.d/99-cryptvm.conf << DRACUT
add_dracutmodules+=" crypt dm rootfs-block "
install_items+=" /etc/crypttab /usr/sbin/cryptsetup "
DRACUT

            echo "Dracut config:"
            cat /etc/dracut.conf.d/99-cryptvm.conf

            # Verify /etc/crypttab is correct (dracut reads this to know which
            # devices to unlock — the kernel cmdline rd.luks.uuid tells it
            # which UUID, and crypttab tells it the mapping name)
            echo "Crypttab:"
            cat /etc/crypttab

            dracut --force --regenerate-all 2>&1 || dracut --force 2>&1 || true

            # Verify the initramfs actually contains cryptsetup
            echo "Checking initramfs contents for cryptsetup..."
            LATEST_INITRD=$(ls -1 /boot/initramfs-*.img 2>/dev/null | sort -V | tail -1)
            if [ -n "$LATEST_INITRD" ]; then
                lsinitrd "$LATEST_INITRD" 2>/dev/null | grep -i crypt | head -5 || true
            fi
        fi

        echo "=== Configuring GRUB ==="

        if [ "{os_family}" = "debian" ]; then
            echo "Using Debian/Ubuntu update-grub workflow"

            sleep 2
            ROOT_UUID=$(blkid -s UUID -o value /dev/mapper/cryptroot 2>/dev/null || echo "")
            echo "Root filesystem UUID: $ROOT_UUID"

            if [ -z "$ROOT_UUID" ]; then
                ROOT_UUID=$(lsblk -no UUID /dev/mapper/cryptroot 2>/dev/null || echo "")
                echo "Fallback UUID detection: $ROOT_UUID"
            fi

            if [ -f /etc/default/grub ]; then
                cp /etc/default/grub /etc/default/grub.backup
                if [ "{"true" if not enable_serial else "false"}" = "true" ]; then
                    echo "Stripping serial console from existing GRUB config..."
                    sed -i 's/console=ttyS[0-9]*[^ ]*//g' /etc/default/grub
                    sed -i 's/earlyprintk=ttyS[0-9]*[^ ]*//g' /etc/default/grub
                    sed -i 's/consoleblank=0//g' /etc/default/grub
                    sed -i 's/  */ /g' /etc/default/grub
                    sed -i 's/ *$//g' /etc/default/grub
                fi
            fi

            cat > /etc/default/grub << GRUB_DEFAULT
GRUB_DEFAULT=0
GRUB_TIMEOUT=10
GRUB_DISTRIBUTOR=$(lsb_release -i -s 2>/dev/null || echo Debian)
GRUB_CMDLINE_LINUX_DEFAULT="quiet"
GRUB_CMDLINE_LINUX="root=/dev/mapper/cryptroot"
{"GRUB_TERMINAL=console" if not enable_serial else 'GRUB_TERMINAL="serial console"\\nGRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"'}
GRUB_DISABLE_RECOVERY="true"
GRUB_DISABLE_OS_PROBER="true"
GRUB_ENABLE_CRYPTODISK=y
GRUB_DISABLE_LINUX_UUID="true"
GRUB_DEFAULT

            if ! grep -q "UUID=$ROOT_UUID" /etc/fstab; then
                echo "Fixing /etc/fstab root entry..."
                sed -i "s|^[^ ]* / |UUID=$ROOT_UUID / |" /etc/fstab
            fi

            if [ "{"true" if not enable_serial else "false"}" = "true" ]; then
                echo "Stripping serial console from all GRUB config sources..."

                # Clean /etc/grub.d/ scripts
                for script in /etc/grub.d/*; do
                    if [ -f "$script" ] && [ -x "$script" ]; then
                        sed -i 's/console=ttyS[0-9]*[,0-9]*//g' "$script"
                        sed -i 's/earlyprintk=ttyS[0-9]*[,0-9]*//g' "$script"
                        sed -i 's/consoleblank=0//g' "$script"
                    fi
                done

                # Clean /etc/default/grub.d/ drop-in files — cloud images put
                # serial console config here and it survives update-grub
                if [ -d /etc/default/grub.d ]; then
                    for f in /etc/default/grub.d/*.cfg /etc/default/grub.d/*.conf; do
                        [ -f "$f" ] || continue
                        echo "  Cleaning $f"
                        sed -i 's/console=ttyS[0-9]*[,0-9]*//g' "$f"
                        sed -i 's/earlyprintk=ttyS[0-9]*[,0-9]*//g' "$f"
                        sed -i 's/consoleblank=0//g' "$f"
                        # Also strip any GRUB_SERIAL_COMMAND or serial terminal config
                        sed -i '/GRUB_SERIAL_COMMAND/d' "$f"
                        sed -i 's/GRUB_TERMINAL=.*/GRUB_TERMINAL=console/' "$f"
                        # Clean up double spaces
                        sed -i 's/  */ /g' "$f"
                        sed -i 's/ *"/"/g' "$f"
                    done
                fi

                # Also clean /etc/kernel/cmdline if it exists (used by some systems)
                if [ -f /etc/kernel/cmdline ]; then
                    sed -i 's/console=ttyS[0-9]*[,0-9]*//g' /etc/kernel/cmdline
                fi
            fi

            echo "Running update-grub to generate configuration..."
            update-grub 2>&1 || true

            if [ "{"true" if not enable_serial else "false"}" = "true" ]; then
                echo "Post-processing generated GRUB config..."
                if [ -f /boot/grub/grub.cfg ]; then
                    cp /boot/grub/grub.cfg /boot/grub/grub.cfg.backup
                    sed -i '/^[[:space:]]*linux/ s/console=ttyS[0-9]*[^ ]*//g' /boot/grub/grub.cfg
                    sed -i '/^[[:space:]]*linux/ s/earlyprintk=ttyS[0-9]*[^ ]*//g' /boot/grub/grub.cfg
                    sed -i '/^[[:space:]]*linux/ s/consoleblank=0//g' /boot/grub/grub.cfg
                    sed -i '/^[[:space:]]*linux/ s/root=[^ ]* root=/root=/g' /boot/grub/grub.cfg
                    sed -i '/^[[:space:]]*linux/ s/  */ /g' /boot/grub/grub.cfg
                    sed -i '/^[[:space:]]*linux/ s/ *$//' /boot/grub/grub.cfg
                fi
            fi

            echo "Generated GRUB config preview:"
            grep -A5 -B5 "linux.*root=" /boot/grub/grub.cfg || true

        else
            echo "Using RHEL grub2-mkconfig workflow"

            # For RHEL 9+/AlmaLinux 9+, Boot Loader Specification (BLS) is the
            # default. The kernel cmdline lives in /boot/loader/entries/*.conf,
            # NOT in grub.cfg. grub.cfg just loads the BLS entries.
            # We must update BOTH grub.cfg AND the BLS entries.

            LUKS_CMDLINE="rd.luks.uuid={luks_uuid} rd.luks.name={luks_uuid}=cryptroot root=/dev/mapper/cryptroot"

            # Update BLS entries directly — this is what actually controls
            # the kernel command line on modern RHEL
            echo "Checking for BLS entries..."
            if [ -d /boot/loader/entries ]; then
                echo "BLS entries found:"
                ls -la /boot/loader/entries/

                for entry in /boot/loader/entries/*.conf; do
                    [ -f "$entry" ] || continue
                    echo "Updating BLS entry: $entry"

                    # Check if options line already has rd.luks.uuid
                    if grep -q "rd.luks.uuid" "$entry"; then
                        echo "  Already has LUKS params, skipping"
                    else
                        # Append LUKS params to the options line
                        sed -i "s|^options \\(.*\\)|options \\1 $LUKS_CMDLINE|" "$entry"
                        echo "  Added LUKS params to options line"
                    fi

                    # Show the result
                    grep "^options" "$entry"
                done
            fi

            if command -v grub2-mkconfig >/dev/null 2>&1; then
                GRUB_CFG="/boot/grub2/grub.cfg"
                [ "{boot_mode}" = "uefi" ] && GRUB_CFG="/boot/efi/EFI/almalinux/grub.cfg"
                [ "{boot_mode}" = "uefi" ] && [ ! -d /boot/efi/EFI/almalinux ] && \\
                    GRUB_CFG="/boot/grub2/grub.cfg"

                echo "Generating GRUB config at $GRUB_CFG..."
                # --update-bls-cmdline propagates GRUB_CMDLINE_LINUX to BLS entries
                grub2-mkconfig -o "$GRUB_CFG" --update-bls-cmdline 2>&1 || \\
                grub2-mkconfig -o "$GRUB_CFG" 2>&1 || true

                # Verify BLS entries have LUKS params after grub2-mkconfig
                if [ -d /boot/loader/entries ]; then
                    echo "Verifying BLS entries after grub2-mkconfig..."
                    for entry in /boot/loader/entries/*.conf; do
                        [ -f "$entry" ] || continue
                        if ! grep -q "rd.luks.uuid" "$entry"; then
                            echo "  WARNING: $entry still missing LUKS params, fixing..."
                            sed -i "s|^options \\(.*\\)|options \\1 $LUKS_CMDLINE|" "$entry"
                        fi
                        echo "  $(basename $entry): $(grep '^options' $entry)"
                    done
                fi

                # Also verify grub.cfg itself
                echo "Verifying GRUB config has LUKS parameters..."
                if grep -q "rd.luks.uuid" "$GRUB_CFG" 2>/dev/null; then
                    echo "OK: rd.luks.uuid found in grub.cfg"
                elif [ -d /boot/loader/entries ] && grep -rq "rd.luks.uuid" /boot/loader/entries/ 2>/dev/null; then
                    echo "OK: rd.luks.uuid found in BLS entries (grub.cfg uses BLS)"
                else
                    echo "WARNING: rd.luks.uuid NOT found anywhere, writing manual fallback..."
                    VMLINUZ=$(ls -1 /boot/vmlinuz-* 2>/dev/null | sort -V | tail -1)
                    [ -z "$VMLINUZ" ] && [ -f /boot/vmlinuz ] && VMLINUZ="/boot/vmlinuz"
                    INITRD=$(ls -1 /boot/initramfs-*.img 2>/dev/null | sort -V | tail -1)
                    [ -z "$INITRD" ] && [ -f /boot/initrd.img ] && INITRD="/boot/initrd.img"

                    if [ -n "$VMLINUZ" ]; then
                        VMLINUZ_BASE=$(basename "$VMLINUZ")
                        INITRD_BASE=$(basename "$INITRD")

                        if [ "{boot_mode}" = "uefi" ]; then
                            cat > "$GRUB_CFG" << GRUBCFG
set timeout=10
set default=0

menuentry "{os_name} (encrypted root)" {{
    insmod part_gpt
    insmod fat
    insmod ext2
    set root='(hd0,gpt2)'
    linux /$VMLINUZ_BASE $LUKS_CMDLINE ro quiet
    initrd /$INITRD_BASE
}}
GRUBCFG
                        else
                            cat > "$GRUB_CFG" << GRUBCFG
set timeout=10
set default=0

menuentry "{os_name} (encrypted root)" {{
    insmod part_msdos
    insmod ext2
    set root='(hd0,msdos1)'
    linux /$VMLINUZ_BASE $LUKS_CMDLINE ro quiet
    initrd /$INITRD_BASE
}}
GRUBCFG
                        fi
                        echo "Written manual fallback grub.cfg"
                    fi
                fi

                echo "GRUB config preview:"
                grep -A3 "linux.*root=\\|^options" "$GRUB_CFG" 2>/dev/null || true
                echo "BLS entries preview:"
                grep "^options" /boot/loader/entries/*.conf 2>/dev/null || echo "No BLS entries"
            fi
        fi

        echo "=== GRUB configuration complete ==="
        echo "=== Chroot setup complete ==="
    """)
