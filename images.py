"""
Cloud image catalog — download URLs and metadata for supported OS images.
All URLs verified as of March 2026.
"""

IMAGES = {
    "debian-12": {
        "name": "Debian 12 (Bookworm)",
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
        "filename": "debian-12-generic-amd64.qcow2",
        "format": "qcow2",
        "os_family": "debian",
    },
    "ubuntu-2404": {
        "name": "Ubuntu 24.04 LTS (Noble)",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "filename": "noble-server-cloudimg-amd64.img",
        "format": "qcow2",
        "os_family": "debian",
    },
    "alma-9": {
        "name": "AlmaLinux 9",
        "url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "filename": "AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "format": "qcow2",
        "os_family": "redhat",
    },
    "rocky-9": {
        "name": "Rocky Linux 9",
        "url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        "filename": "Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        "format": "qcow2",
        "os_family": "redhat",
    },
}
