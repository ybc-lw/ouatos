# ouatos

ouatos is a lightweight Linux distribution based on Debian, designed to minimize operating system resource usage so that ample resources remain available for running language models.

## Included software

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
- **Node.js and pnpm**
- **llama.cpp**

## Installation

**The recommended installation method is to download an installation image from [GitHub Releases](../../releases).**

The installation image performs the following setup tasks:

- Initializes `/etc/machine-id`
- Sets a password for the root user
- Creates a regular user with administrative privileges via `sudo`
- Creates required system directories that are not tracked by Git
- **Configures appropriate ownership and permissions for system files and directories**

## License

ouatos is released under the MIT License.

See LICENSE for the full license text.
