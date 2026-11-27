# ouatos

ouatos is a lightweight Linux distribution based on Debian, designed to minimize system resource usage and leave more resources available for running language models.

[![License](https://img.shields.io/github/license/ybc-lw/ouatos)](LICENSE)
[![Releases](https://img.shields.io/github/v/release/ybc-lw/ouatos)](https://github.com/ybc-lw/ouatos/releases)
[![CI](https://github.com/ybc-lw/ouatos/actions/workflows/ci.yml/badge.svg)](https://github.com/ybc-lw/ouatos/actions/workflows/ci.yml)

## Included Software

The current system image includes the following software and components:

- **Debian base system**
- **systemd**
- **Networking utilities**
- **OpenSSL**
- **Python 3 and pip**
  - PyTorch (CPU inference only)
  - Transformers
  - Tokenizers
  - Safetensors
  - GGUF
  - Hugging Face Hub
- **OpenBLAS**
- **Node.js and pnpm**
- **llama.cpp**
- **Git**

## Build

Use `scripts/build.py` to build the root filesystem (`rootfs`), then use `installer/build_iso.py` to create the installation ISO.

Prebuilt installation images are also available for download from [GitHub Releases](../../releases).

## Supported Architecture

- AMD64 UEFI only

## License

ouatos is released under the MIT License. See LICENSE for the full license text.
