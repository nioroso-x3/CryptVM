#!/usr/bin/env python3
"""
CryptVM Builder CLI - Command line interface for building encrypted VM images
"""

import os
import sys
import argparse
from pathlib import Path
from builder import build_image, check_requirements
from downloader import ensure_cloud_image, convert_qcow2_to_raw
from images import IMAGES


def get_password_from_env_or_prompt(env_var: str, prompt: str) -> str:
    """Get password from environment variable or prompt user"""
    password = os.getenv(env_var)
    if password:
        print(f"Using {env_var} from environment")
        return password

    import getpass
    return getpass.getpass(prompt)


def main():
    parser = argparse.ArgumentParser(
        description="Build encrypted VM disk images from cloud images",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage
  sudo python3 build-image.py debian-12 --pubkey ~/.ssh/id_rsa.pub --output debian.img

  # UEFI mode with custom size
  sudo python3 build-image.py ubuntu-2404 --pubkey ~/.ssh/id_rsa.pub --boot-mode uefi --size 20480 --output ubuntu-uefi.img

  # Using environment variables for passwords
  export LUKS_PASSWORD="mysecretpass"
  export ROOT_PASSWORD="rootpass"
  sudo -E python3 build-image.py alma-9 --pubkey ~/.ssh/id_rsa.pub --output alma.img

Available OS images:""" + "\n  " + "\n  ".join([f"{key}: {info['name']}" for key, info in IMAGES.items()])
    )

    parser.add_argument(
        "os_image",
        choices=list(IMAGES.keys()),
        help="OS image to use"
    )

    parser.add_argument(
        "--pubkey",
        type=Path,
        required=True,
        help="SSH public key file for root access"
    )

    parser.add_argument(
        "--output", "-o",
        type=Path,
        default="output.img",
        help="Output image file (default: output.img)"
    )

    parser.add_argument(
        "--boot-mode",
        choices=["bios", "uefi"],
        default="uefi",
        help="Boot mode: bios (MBR) or uefi (GPT) (default: uefi)"
    )

    parser.add_argument(
        "--size",
        type=int,
        default=10240,
        help="Disk size in MB (default: 10240)"
    )

    parser.add_argument(
        "--luks-password",
        help="LUKS encryption password (or set LUKS_PASSWORD env var)"
    )

    parser.add_argument(
        "--root-password",
        help="Root user password (or set ROOT_PASSWORD env var)"
    )

    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Check dependencies and exit"
    )

    args = parser.parse_args()

    # Check if running as root
    if os.geteuid() != 0:
        print("Error: Must run as root (use sudo)")
        sys.exit(1)

    # Check dependencies
    if args.check_deps:
        print("Checking dependencies...")
        missing_bios = check_requirements("bios")
        missing_uefi = check_requirements("uefi")

        if missing_bios:
            print(f"BIOS mode missing: {', '.join(missing_bios)}")
        else:
            print("BIOS mode: ✓ All dependencies satisfied")

        if missing_uefi:
            print(f"UEFI mode missing: {', '.join(missing_uefi)}")
        else:
            print("UEFI mode: ✓ All dependencies satisfied")

        if missing_bios or missing_uefi:
            all_missing = sorted(set(missing_bios + missing_uefi))
            print(f"\nInstall missing packages:")
            print(f"  sudo apt install {' '.join(all_missing)}")
            sys.exit(1)
        sys.exit(0)

    # Validate SSH public key file
    if not args.pubkey.exists():
        print(f"Error: SSH public key file not found: {args.pubkey}")
        sys.exit(1)

    try:
        ssh_pubkey = args.pubkey.read_text().strip()
        if not ssh_pubkey or not any(ssh_pubkey.startswith(p) for p in ["ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2"]):
            print(f"Error: Invalid SSH public key in {args.pubkey}")
            sys.exit(1)
    except Exception as e:
        print(f"Error reading SSH public key: {e}")
        sys.exit(1)

    # Get passwords
    luks_password = args.luks_password or get_password_from_env_or_prompt(
        "LUKS_PASSWORD", "LUKS encryption password: "
    )

    root_password = args.root_password or get_password_from_env_or_prompt(
        "ROOT_PASSWORD", "Root user password: "
    )

    if len(luks_password) < 8:
        print("Error: LUKS password must be at least 8 characters")
        sys.exit(1)

    # Check dependencies for selected boot mode
    missing = check_requirements(args.boot_mode)
    if missing:
        print(f"Error: Missing required tools for {args.boot_mode} mode: {', '.join(missing)}")
        print(f"Install with: sudo apt install {' '.join(missing)}")
        sys.exit(1)

    # Get OS info
    os_info = IMAGES[args.os_image]
    print(f"Building {os_info['name']} image:")
    print(f"  Boot mode: {args.boot_mode.upper()}")
    print(f"  Size: {args.size} MB")
    print(f"  Output: {args.output}")
    print()

    try:
        # Download and convert cloud image
        print("Downloading cloud image...")
        cloud_img = ensure_cloud_image(args.os_image)
        print(f"Cloud image: {cloud_img}")

        print("Converting to raw format...")
        cloud_raw = convert_qcow2_to_raw(cloud_img)
        print(f"Raw image: {cloud_raw}")

        # Build the encrypted image
        print("Building encrypted image...")
        success = build_image(
            cloud_image_raw=cloud_raw,
            output_path=args.output.resolve(),
            disk_size_mb=args.size,
            luks_password=luks_password,
            root_password=root_password,
            ssh_pubkey=ssh_pubkey,
            os_family=os_info["os_family"],
            boot_mode=args.boot_mode,
            os_name=os_info["name"],
            log=print,
        )

        if success:
            print(f"\n✓ Image build complete!")
            print(f"  Output: {args.output}")
            print(f"  Size: {args.output.stat().st_size // (1024*1024)} MB")
            print(f"\nBoot with:")
            if args.boot_mode == "uefi":
                print(f"  qemu-system-x86_64 -hda {args.output} -m 1024 -bios /usr/share/ovmf/OVMF.fd")
            else:
                print(f"  qemu-system-x86_64 -hda {args.output} -m 1024")
        else:
            print("\n✗ Image build failed!")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nBuild cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
