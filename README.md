# ouatos

ouatos is a lightweight Linux distribution based on Debian, designed to minimize operating system resource usage so that ample resources remain available for running language models.

## Included software

The current system image includes the following software and components:

- **Debian base system**
- **systemd**
- **APT and dpkg**
- **Bash**
- **iproute2**
- **iputils-ping**
- **curl**
- **OpenSSL**
- **Python 3 and pip**
- **Node.js and npm**
- **llama.cpp**

## Installation

**The recommended installation method is to download an installation image from [GitHub Releases](../../releases).**

The installation image performs the following setup tasks:

- Initializes `/etc/machine-id`
- Sets a password for the root user
- Creates a regular user with administrative privileges via `sudo`
- **Configures appropriate ownership and permissions for system files and directories**

### Filesystem Ownership and Permissions

- The root directory (`/`) and all system directories and files under it are owned by `root:root` by default, unless otherwise specified below.
- The `/tmp` and `/var/tmp` directories are owned by `root:root` and have permissions set to `1777`, allowing all users to create, read, and write files as permitted by file ownership and individual file permissions. The sticky bit prevents users from deleting or renaming files owned by other users.
- Each user's home directory under `/home` is owned by that user and their primary group (e.g., `/home/<username>` is owned by `<username>:<username>`).

## License

ouatos is released under the MIT License.

See LICENSE for the full license text.
