# CryptVM Builder

Vibe coded tool to build BIOS-bootable VM disk images with LUKS-encrypted root partitions from
cloud images. Runs directly on Linux or WSL2 as root.

Coded in one shot using Claude Opus 4.6 and some manual fixes.

Easily automates creating encrypted images to use in any public cloud.

## Features

- LUKS1 encrypted root partition, password-protected at boot
- MBR + BIOS boot (no UEFI)
- SSH root access via public key (password auth disabled)
- Custom root password
- Interactive TUI

## Supported OS Images

- Debian 12 (Bookworm)
- Ubuntu 24.04 LTS (Noble)
- AlmaLinux 9
- Rocky Linux 9

## Prerequisites

```bash
# Debian/Ubuntu
sudo apt install qemu-utils cryptsetup parted e2fsprogs grub-pc-bin python3-pip

# Fedora/RHEL
sudo dnf install qemu-img cryptsetup parted e2fsprogs grub2-pc python3-pip

pip install textual
```

## Usage

```bash
sudo python3 cryptvm.py
```

The TUI guides you through selecting an OS, setting passwords, and providing
your SSH key. The tool downloads the cloud image (cached), converts it, and
builds the encrypted disk image directly using losetup/cryptsetup/chroot.

## Booting the Image

```bash
qemu-system-x86_64 -hda output.img -m 1024

# With SSH forwarding:
qemu-system-x86_64 \
    -hda output.img -m 2048 \
    -netdev user,id=net0,hostfwd=tcp::2222-:22 \
    -device virtio-net-pci,netdev=net0

ssh -p 2222 root@localhost
```

You will be prompted for the LUKS passphrase once at boot by the initramfs.

## Disk Layout

    MBR partition table
    ├── Partition 1: /boot (ext4, 512MB, unencrypted)
    │   Kernel, initramfs, GRUB
    └── Partition 2: LUKS1 container (remainder)
        └── ext4 filesystem: /
            Full OS root, /root/.ssh/authorized_keys

## Files

    cryptvm.py     - TUI entry point
    builder.py     - Core build logic (losetup, cryptsetup, chroot)
    downloader.py  - Cloud image download with resume
    images.py      - OS image URL catalog

## License

MIT
