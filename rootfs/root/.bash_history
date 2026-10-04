apt install --no-install-recommends iproute2 iputils-ping
ping -c 3 deb.debian.org
apt update
apt install --no-install-recommends systemd systemd-sysv
apt install --no-install-recommends -y python3-minimal python3-pip
apt install --no-install-recommends -y nodejs npm
apt autoremove
sudo rm -rf rootfs/usr/share/man/*
sudo rm -rf rootfs/usr/share/doc/*
sudo rm -rf rootfs/usr/share/info/*
rm -rf /usr/share/man/*
rm -rf /usr/share/doc/*
rm -rf /usr/share/info/*
sudo find rootfs/usr/share/locale -mindepth 1 -maxdepth 1   ! -name 'C'   ! -name 'C.UTF-8'   ! -name 'C.utf8'   ! -name 'en_US*'   ! -name 'zh_CN*'   ! -name 'ja_JP*'   -exec rm -rf {} +
find /usr/share/locale -mindepth 1 -maxdepth 1   ! -name 'C'   ! -name 'C.UTF-8'   ! -name 'C.utf8'   ! -name 'en_US*'   ! -name 'zh_CN*'   ! -name 'ja_JP*'   -exec rm -rf {} +
apt update
apt-cache search llama.cpp
apt-cache search llama.cpp
exit
llama-cli --version
llama-completion --version
ls -lh /usr/local/lib/libllama-completion-impl.so
LD_LIBRARY_PATH=/usr/local/lib llama-completion --version
echo '/usr/local/lib' > /etc/ld.so.conf.d/usr-local.conf
ldconfig
llama-completion --version
exit
rm -f /usr/local/bin/llama-completion
rm -f /usr/local/lib/libllama*.so*
rm -f /usr/local/lib/libggml*.so*
rm -f /usr/lib/x86_64-linux-gnu/libgomp.so.1
rm -f /usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0
ls -la /usr/local/bin
ls -la /usr/local/lib
rm -f /etc/ld.so.conf.d/usr-local.conf
du -sh /
apt autoremove
apt clean
sudo apt autoremove --purge && sudo apt clean && sudo apt autoclean
apt autoremove --purge && sudo apt clean && sudo apt autoclean
apt autoremove --purge && apt clean && apt autoclean
du -sh /
pip update
apt update
apt install curl
sudo apt install --no-install-recommends curl
apt install --no-install-recommends curl
apt autoremove --purge && apt clean && apt autoclean
apt clean
exit
ldconfig
ldd /usr/local/bin/llama-cli | grep 'not found'
ldd /usr/local/bin/llama-server | grep 'not found'
llama-cli --version
llama-server --version
du -xh -d 1 / 2>/dev/null | sort -h
du -xh -d 1 /usr 2>/dev/null | sort -h
du -xh -d 1 /var 2>/dev/null | sort -h
du -xh -d 1 /usr/share 2>/dev/null | sort -h
du -xh -d 1 /usr/lib 2>/dev/null | sort -h
du -xh -d 2 /usr/local 2>/dev/null | sort -h
du -xh -d 1 /usr/share/nodejs 2>/dev/null | sort -h
du -xh -d 1 /usr/lib/x86_64-linux-gnu 2>/dev/null | sort -h
dpkg-query -W -f='${Installed-Size}\t${Package}\n' 2>/dev/null | sort -n | tail -50
dpkg -l | grep -E '^(ii|rc)  (npm|node-|nodejs)'
apt-get -s purge npm webpack eslint libnode-dev libssl-dev
apt purge npm webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
du -sh /
du -sh /usr/share/nodejs
du -xh -d 1 /usr/lib/x86_64-linux-gnu 2>/dev/null | sort -h
du -xh -d 1 /usr/lib/python3.13 2>/dev/null | sort -h
npm --version
apt install npm
apt update
apt install --no-install-recommends npm
apt install --no-install-recommends npm
apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
du -sh /
du -sh /usr/share/nodejs
apt autoremove --purge
apt clean
apt autoclean
du -sh /
du -xh -d 1 /usr/lib/x86_64-linux-gnu 2>/dev/null | sort -hr | head -30
du -xh -d 1 /usr/lib/python3.13 2>/dev/null | sort -hr | head -20
find /usr/lib/x86_64-linux-gnu -maxdepth 1 -type f -printf '%s\t%p\n' 2>/dev/null   | sort -nr   | head -30   | numfmt --field=1 --to=iec
rm -rf /usr/lib/python3.13/__pycache__
rm -rf /usr/lib/python3.13/test
dpkg-query -W -f='${Package}\t${Installed-Size}\n' 2>/dev/null   | sort -k2 -n   | tail -30
apt autoremove --purge
apt-mark showmanual
npm install -g @anthropic-ai/claude-code
npm --version
sudo apt install --no-install-recommends
sudo apt install --no-install-recommends npm
apt install --no-install-recommends npm
apt update
apt install --no-install-recommends npm
apt autoremove
apt clean
rm -rf /usr/lib/python3.13/__pycache__
rm -rf /usr/lib/python3.13/test
rm -rf /usr/lib/python3.13/__pycache__
rm -rf /usr/lib/python3.13/test
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
du -sh /
du -sh /usr/share/nodejs
apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
npm install -g @anthropic-ai/claude-code
npm --version
apt update
apt install npm
apt install --no-installrecommends npm
apt install --no-instal-lrecommends npm
apt install --no-install-recommends npm
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
apt clean
apt autoclean
du -sh /
npm cache clean --force
apt apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
npm --version
apt update
apt install --no-install-recommends npm
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
du -sh /
apt purge libssl-dev
apt update
apt install npm
apt install --no-install-recommends npm
apt purge git
sudo apt purge --auto-remove libssl-dev libnode-dev
apt purge --auto-remove libssl-dev libnode-dev
sudo apt-mark manual npm node-gyp
sudo apt purge libssl-dev libnode-dev
apt-mark manual npm node-gyp
apt purge libssl-dev libnode-dev
apt-mark manual npm node-gyp
apt purge --auto-remove libssl-dev libnode-dev
apt autoremove --purge
dpkg --purge --force-depends libssl-dev libnode-dev
apt install -f
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
du -sh /
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
apt-get -s purge npm webpack eslint libnode-dev libssl-dev
apt purge webpack eslint
apt purge webpack
dpkg-query -W -f='${Package}\t${Installed-Size}\n' 2>/dev/null   | sort -k2 -n   | tail -30
clean
apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
apt purge webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
exit
/usr/local/bin/node --version
/usr/local/bin/npm --version
/usr/local/bin/npx --version
node --version
npm --version
npx --version
exit
node --version
npm --version
npx --version
apt clean
apt autoremove
llama-cli --version
llama-server --version
apt purge npm webpack eslint libnode-dev libssl-dev node-css-loader node-gyp
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
du -sh /
du -sh /usr/share/nodejs
node --version
npm --version
npx --version
apt-get -s purge nodejs npm node-corepack
apt-get -s autoremove --purge
apt purge nodejs npm node-corepack
apt autoremove --purge
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
node --version
npm --version
npx --version
du -sh /
du -sh /usr/share/nodejs 2>/dev/null
du -xh -d 1 /usr 2>/dev/null | sort -h
du -xh -d 1 /usr/lib 2>/dev/null | sort -h
du -xh -d 1 /usr/share 2>/dev/null | sort -h
du -xh -d 2 /usr/local 2>/dev/null | sort -h
du -xh -d 2 /usr/lib/x86_64-linux-gnu 2>/dev/null | sort -h
du -xh -d 1 /usr/lib/python3.13 2>/dev/null | sort -h
find /usr/lib/x86_64-linux-gnu -maxdepth 1 -type f -printf '%s\t%p\n'   | sort -n | tail -30
find /usr/lib/x86_64-linux-gnu -maxdepth 1 -type l -printf '%p -> %l\n'   | sort
dpkg -S /usr/lib/x86_64-linux-gnu/libgssapi_krb5.so.2 /usr/lib/x86_64-linux-gnu/libkrb5.so.3 /usr/lib/x86_64-linux-gnu/libldap.so.2 /usr/lib/x86_64-linux-gnu/libssh2.so.1
apt-mark showmanual
apt-cache depends curl
apt-cache depends libcurl4
apt-cache rdepends libgssapi-krb5-2
apt-cache rdepends libkrb5-3
apt-cache rdepends libldap2
apt-cache rdepends libssh2-1t64
curl apple.com
apt-cache depends libcurl4t64
dpkg-query -W -f='${Installed-Size}\t${Package}\n'   curl libcurl4t64 libgssapi-krb5-2 libkrb5-3 libldap2 libssh2-1t64
du -xh -d 2 /usr/local/lib 2>/dev/null | sort -h
du -xh -d 2 /usr/local/lib 2>/dev/null | sort -h
du -xh -d 1 /usr/lib/python3/dist-packages 2>/dev/null | sort -h
file /usr/local/bin/node
readelf -S /usr/local/bin/node | grep -E '\.debug|\.symtab'
file /usr/local/bin/node
readelf -S /usr/local/bin/node | grep -E '\.debug|\.symtab'
ls -lh /usr/local/bin/node
du -xh -d 1 /usr/bin 2>/dev/null | sort -h | tail -50
find /usr/bin -maxdepth 1 -type f -printf '%s\t%p\n' | sort -n | tail -30
du -xh -d 1 /var/lib 2>/dev/null | sort -h
python3 - <<'PY'
import sys
print(sys.version)
print(sys.prefix)
PY

clean
clear
dpkg-query -W -f='${Installed-Size}\t${Package}\n'   python3.13 python3.13-minimal python3-minimal python3-pip
dpkg -L python3.13-minimal | wc -l
dpkg -L python3.13 | wc -l
dpkg -L python3.13 | grep '^/usr/lib/python3.13/' |   sed 's#/usr/lib/python3.13/##' | cut -d/ -f1 | sort -u
clear
dpkg-query -W -f='${Installed-Size}\t${Package}\n' libpython3.13-stdlib
dpkg -L libpython3.13-stdlib | wc -l
dpkg -L libpython3.13-stdlib | head -50
dpkg -S   /usr/lib/python3.13/email/__init__.py   /usr/lib/python3.13/asyncio/__init__.py   /usr/lib/python3.13/encodings/__init__.py   /usr/lib/python3.13/unittest/__init__.py
clear
apt-cache depends python3-pip
apt-cache depends python3.13
apt-cache depends libpython3.13-stdlib
find /usr/lib/python3.13 -type d -name '__pycache__' -prune -exec rm -rf {} +
du -sh /usr/lib/python3.13
clear
du -xh -d 1 /usr/lib/python3.13 2>/dev/null | sort -h
du -xh -d 2 /usr/lib/python3/dist-packages/pip 2>/dev/null | sort -h
find /usr/lib/python3/dist-packages/pip -type d -name '__pycache__' -prune -exec rm -rf {} +
du -sh /usr/lib/python3/dist-packages/pip
find /usr/lib/python3.13/pydoc_data -type f -printf '%s\t%p\n' | sort -n | tail
find /usr/lib/python3.13/lib-dynload -type f -printf '%s\t%f\n' | sort -n
rm -f /usr/lib/python3.13/lib-dynload/_test*.so
rm -f /usr/lib/python3.13/lib-dynload/xxlimited*.so
rm -f /usr/lib/python3.13/lib-dynload/xxsubtype*.so
rm -f /usr/lib/python3.13/lib-dynload/_xxtestfuzz*.so
du -sh /usr/lib/python3.13/lib-dynload
rm -rf /usr/lib/python3.13/pydoc_data
clear
du -xh -d 1 /usr/lib/x86_64-linux-gnu 2>/dev/null | sort -h
dpkg-query -W -f='${Installed-Size}\t${Package}\n' 2>/dev/null   | sort -n | tail -40
dpkg-query -W -f='${Installed-Size}\t${Package}\n' 2>/dev/null   | grep -E '^(.*\t)(lib|gcc|openssl|curl)'   | sort -n | tail -40
exit
node --version
npm --version
npx --version
du -sh /
ls -lh /usr/local/bin/node
du -xh -d 1 /usr/local/lib 2>/dev/null | sort -h
ls -lh /usr/local/bin/llama-cli /usr/local/bin/llama-server
clear
du -xh /usr/local/lib/*.so* 2>/dev/null | sort -h
file /usr/local/bin/llama-cli
file /usr/local/bin/llama-server
file /usr/local/lib/*.so* 2>/dev/null | grep -E 'not stripped|debug'
chown root:root /usr/local/bin/llama-cli /usr/local/bin/llama-server
chmod 755 /usr/local/bin/llama-cli /usr/local/bin/llama-server
chown -R root:root /usr/local/lib
clear
exit
apt update
apt install file
apt --no-install-recommends file
apt install --no-install-recommends file
apt clean
apt autoremove
apt autoclean
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
du -sh /
clear
exit
llama-cli --version
llama-server --version
ldd /usr/local/bin/llama-cli | grep 'not found'
ldd /usr/local/bin/llama-server | grep 'not found'
du -sh /
clear
du -xh /usr/local/lib/*.so* 2>/dev/null | sort -h
file /usr/local/lib/*.so* 2>/dev/null | grep -E 'not stripped|debug'
chown root:root /usr/local/bin/llama-cli /usr/local/bin/llama-server
chmod 755 /usr/local/bin/llama-cli /usr/local/bin/llama-server
chown -R root:root /usr/local/lib
rm -rf /usr/local/lib/nodejs/npm/docs
rm -rf /usr/local/lib/nodejs/npm/man
rm -rf /usr/lib/python3.13/pydoc_data
rm -rf /usr/share/doc/*
du -sh /
npm --version
node --version
python3 --version
exit
apt update
DEBIAN_FRONTEND=noninteractive apt install -y --no-install-recommends   linux-image-amd64   initramfs-tools   grub-common   grub-pc-bin   grub-efi-amd64-bin   efibootmgr   sudo
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/apt/*
exit
