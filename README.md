# CryptVM Builder

Tool to build BIOS or UEFI-bootable VM disk images with LUKS-encrypted root partitions from
cloud images. Runs directly on Linux or WSL2 as root.

Easily automates creating encrypted images to use in any public cloud.

## Features

- LUKS1 encrypted root partition, password-protected at boot
- **Both BIOS (MBR) and UEFI (GPT) boot modes supported**
- SSH root access via public key (password auth disabled)
- Custom root password
- Interactive TUI
- Early dependency checking with helpful error messages

## Supported OS Images

- Debian 12 (Bookworm)
- Ubuntu 24.04 LTS (Noble)
- AlmaLinux 9
- Rocky Linux 9

## Prerequisites

```bash
# For BIOS mode (Debian/Ubuntu)
sudo apt install qemu-utils cryptsetup parted e2fsprogs grub-pc-bin python3-pip

# For UEFI mode (additional packages)
sudo apt install grub-efi-amd64 grub-efi-amd64-bin efibootmgr dosfstools

# For BIOS mode (Fedora/RHEL)
sudo dnf install qemu-img cryptsetup parted e2fsprogs grub2-pc python3-pip

# For UEFI mode (additional packages)
sudo dnf install grub2-efi-x64 grub2-efi-x64-modules efibootmgr dosfstools

pip install textual
```

You can also check dependencies with the included utility:
```bash
sudo python3 check_deps.py
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
# BIOS mode
qemu-system-x86_64 -hda output.img -m 1024

# UEFI mode
qemu-system-x86_64 -hda output.img -m 1024 -bios /usr/share/ovmf/OVMF.fd

# With SSH forwarding:
qemu-system-x86_64 \
    -hda output.img -m 2048 \
    -netdev user,id=net0,hostfwd=tcp::2222-:22 \
    -device virtio-net-pci,netdev=net0

ssh -p 2222 root@localhost
```

You will be prompted for the LUKS passphrase once at boot by the initramfs.

## Disk Layout

### BIOS Mode
    MBR partition table
    ├── Partition 1: /boot (ext4, 512MB, unencrypted)
    │   Kernel, initramfs, GRUB
    └── Partition 2: LUKS1 container (remainder)
        └── ext4 filesystem: /
            Full OS root, /root/.ssh/authorized_keys

### UEFI Mode
    GPT partition table
    ├── Partition 1: EFI System Partition (fat32, 512MB, unencrypted)
    │   UEFI bootloader, /boot/efi
    ├── Partition 2: /boot (ext4, 512MB, unencrypted)
    │   Kernel, initramfs, GRUB
    └── Partition 3: LUKS1 container (remainder)
        └── ext4 filesystem: /
            Full OS root, /root/.ssh/authorized_keys

## Files

    cryptvm.py     - TUI entry point
    builder.py     - Core build logic (losetup, cryptsetup, chroot)
    downloader.py  - Cloud image download with resume
    images.py      - OS image URL catalog
    check_deps.py  - Standalone dependency checker

## License

MIT
