#!/usr/bin/env python3
"""
Standalone utility checker for CryptVM Builder.
Lists missing required tools and provides installation commands.
"""

import sys
from builder import check_requirements

def main():
    print("CryptVM Builder - Dependency Checker")
    print("=" * 40)

    # Check BIOS mode requirements
    missing_bios = check_requirements("bios")
    print(f"BIOS mode requirements: {'✓ All present' if not missing_bios else '✗ Missing: ' + ', '.join(missing_bios)}")

    # Check UEFI mode requirements
    missing_uefi = check_requirements("uefi")
    print(f"UEFI mode requirements: {'✓ All present' if not missing_uefi else '✗ Missing: ' + ', '.join(missing_uefi)}")

    # Show installation commands if anything is missing
    all_missing = sorted(set(missing_bios + missing_uefi))
    if all_missing:
        print("\nTo install missing utilities:")
        print(f"  sudo apt install {' '.join(all_missing)}")
        print(f"  # OR for RHEL/Fedora:")
        print(f"  sudo dnf install {' '.join(all_missing)}")
        sys.exit(1)
    else:
        print("\n✓ All dependencies satisfied!")
        sys.exit(0)

if __name__ == "__main__":
    main()