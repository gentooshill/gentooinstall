#!/usr/bin/env python3
"""
Gentoo Automated Installer (Python)
Automates the full Gentoo installation process as described in README.md, with user prompts and system command execution.
"""
import subprocess
import sys
import os
import time
import shutil
import stat
import textwrap

USE_LVM = False
DEVICES = {
    "efi": "",
    "root": "",
    "home": "",
    "swap": ""
}
# Add a global to track init system choice
INIT_SYSTEM = None

def run_cmd(cmd, check=True, shell=True, input_text=None):
    print(f"\n[RUN] {cmd}")
    try:
        result = subprocess.run(cmd, shell=shell, check=check, text=True, input=input_text)
        return result
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed: {e}")
        if not yesno("Continue anyway?"):
            sys.exit(1)
        return e

def yesno(prompt):
    while True:
        ans = input(f"{prompt} [y/n]: ").strip().lower()
        if ans in ("y", "yes"): return True
        if ans in ("n", "no"): return False
        print("Please answer y or n.")

def require_root():
    if os.geteuid() != 0:
        print("This script must be run as root.")
        sys.exit(1)

def pause(msg="Press Enter to continue..."):
    input(msg)

def print_section(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")

def prompt_device_paths():
    print_section("Assign Partition Devices")
    DEVICES["efi"] = input("Enter EFI partition device (e.g., /dev/vda1): ").strip()
    DEVICES["root"] = input("Enter root partition device (e.g., /dev/vda2): ").strip()
    if yesno("Do you have a separate home partition?"):
        DEVICES["home"] = input("Enter home partition device (e.g., /dev/vda3): ").strip()
    else:
        DEVICES["home"] = ""
    if yesno("Do you have a swap partition?"):
        DEVICES["swap"] = input("Enter swap partition device (e.g., /dev/vda4): ").strip()
    else:
        DEVICES["swap"] = ""

def unmount_partitions(disk):
    print(f"[INFO] Checking for mounted partitions on {disk}...")
    import subprocess
    import re
    # Get all partitions for the disk (e.g., /dev/sda1, /dev/sda2, ...)
    result = subprocess.run(f"lsblk -ln {disk} | awk '{{print $1}}'", shell=True, capture_output=True, text=True)
    partitions = [f"/dev/{line.strip()}" for line in result.stdout.splitlines() if line.strip() and line.strip() != disk.split('/')[-1]]
    # Find which are mounted
    mounted = []
    with open('/proc/mounts') as f:
        mounts = f.read()
        for part in partitions:
            if part in mounts:
                mounted.append(part)
    # Unmount in reverse order (deepest first)
    for part in reversed(mounted):
        print(f"[INFO] Unmounting {part}...")
        run_cmd(f"umount -lf {part}", check=False)
    # Deactivate swap
    for part in partitions:
        with open('/proc/swaps') as f:
            swaps = f.read()
            if part in swaps:
                print(f"[INFO] Turning off swap on {part}...")
                run_cmd(f"swapoff {part}", check=False)
    # Deactivate LVM
    print("[INFO] Deactivating all LVM volume groups (if any)...")
    run_cmd("vgchange -an", check=False)
    # Close cryptsetup mappings
    print("[INFO] Closing all cryptsetup mappings (if any)...")
    run_cmd("for m in $(ls /dev/mapper | grep -v control); do cryptsetup close $m || true; done", check=False)
    if not mounted:
        print(f"[INFO] No mounted partitions found on {disk}.")

def partition_disk():
    print_section("Disk Partitioning (UEFI, LVM, LUKS)")
    print("Refer to README for details. This will WIPE your disk!")
    disk = input("Enter target disk (e.g., /dev/nvme0n1 or /dev/sda): ").strip()
    unmount_partitions(disk)
    if not yesno(f"Partition and wipe {disk}? THIS WILL ERASE ALL DATA!"):
        return
    # Ask for EFI size
    efi_size = input("EFI partition size? Enter 512M or 1G [512M]: ").strip() or "512M"
    # Ask for swap size
    swap_size = input("Swap partition size? (e.g., 4G, 0 for none) [4G]: ").strip() or "4G"
    # Partition layout: EFI, swap (if any), root (rest)
    parted_cmds = [
        f"mklabel gpt",
        f"mkpart primary fat32 1MiB {efi_size}",
        f"set 1 boot on"
    ]
    if swap_size != "0":
        parted_cmds.append(f"mkpart primary linux-swap {efi_size} {swap_size}")
        root_start = swap_size
    else:
        root_start = efi_size
    parted_cmds.append(f"mkpart primary ext4 {root_start} 100%")
    for cmd in parted_cmds:
        run_cmd(f"parted --script {disk} {cmd}")
    run_cmd(f"parted {disk} print")
    pause()

def setup_luks_lvm():
    global USE_LVM, DEVICES
    print_section("LUKS Encryption and LVM Setup")
    if not yesno("Do you want to use LUKS encryption and LVM?"):
        USE_LVM = False
        return
    USE_LVM = True
    disk = input("Enter LVM partition (e.g., /dev/nvme0n1p2 or /dev/sda2): ").strip()
    run_cmd(f"cryptsetup -v --cipher aes-xts-plain64 --key-size 256 -y luksFormat {disk}")
    run_cmd(f"cryptsetup open --type luks {disk} cryptcontainer")
    run_cmd("pvcreate /dev/mapper/cryptcontainer")
    run_cmd("vgcreate vg0 /dev/mapper/cryptcontainer")
    # Ask for swap size
    swap_size = input("Swap logical volume size? (e.g., 4G, 0 for none) [4G]: ").strip() or "4G"
    run_cmd("lvcreate --size 50G vg0 --name root")
    run_cmd("lvcreate --extents 100%FREE vg0 --name home")
    if swap_size != "0":
        run_cmd(f"lvcreate --size {swap_size} vg0 --name swap")
        DEVICES["swap"] = "/dev/vg0/swap"
    else:
        DEVICES["swap"] = ""
    DEVICES["root"] = "/dev/vg0/root"
    DEVICES["home"] = "/dev/vg0/home"
    run_cmd("lvdisplay")
    pause()

def create_filesystems():
    print_section("Creating Filesystems")
    run_cmd(f"mkfs.vfat -F32 {DEVICES['efi']}")
    if DEVICES["root"]:
        run_cmd(f"mkfs.ext4 {DEVICES['root']}")
    else:
        print("[ERROR] No root partition specified. Skipping mkfs.ext4 for root.")
    if DEVICES["home"]:
        run_cmd(f"mkfs.ext4 {DEVICES['home']}")
    if DEVICES["swap"]:
        run_cmd(f"mkswap {DEVICES['swap']}")
    pause()

def mount_filesystems():
    print_section("Mounting Filesystems")
    run_cmd("mkdir -p /mnt/gentoo")
    run_cmd(f"mount {DEVICES['root']} /mnt/gentoo")
    run_cmd("mkdir -p /mnt/gentoo/boot")
    run_cmd(f"mount {DEVICES['efi']} /mnt/gentoo/boot")
    if DEVICES["home"]:
        run_cmd("mkdir -p /mnt/gentoo/home")
        run_cmd(f"mount {DEVICES['home']} /mnt/gentoo/home")
    if DEVICES["swap"]:
        run_cmd(f"swapon {DEVICES['swap']}")
    pause()

def set_time():
    print_section("Setting Date and Time")
    run_cmd("date")
    import os
    # Timezone selection
    print("Please enter your timezone in the format Region/City (e.g., Europe/Bucharest, America/New_York).")
    print("You can find valid timezones in /usr/share/zoneinfo or at https://en.wikipedia.org/wiki/List_of_tz_database_time_zones")
    tz = input("Enter your timezone: ").strip()
    # Save to /mnt/gentoo/etc/timezone if it exists, else /etc/timezone
    timezone_path = "/mnt/gentoo/etc/timezone" if os.path.exists("/mnt/gentoo/etc") else "/etc/timezone"
    with open(timezone_path, "w") as f:
        f.write(tz + "\n")
    print(f"[INFO] Timezone '{tz}' written to {timezone_path}")
    # Try ntpdate, but if not found, prompt user to set date manually
    import shutil
    if shutil.which("ntpdate"):
        run_cmd("ntpdate pool.ntp.org")
    else:
        print("[INFO] ntpdate is not available in this environment.")
        print("You can set the system date and time manually with the following command:")
        print("  date MMDDhhmmYYYY")
        print("For example, to set July 9, 2025, 16:30, type: date 070916302025")
        manual = input("Would you like to set the date/time now? [y/n]: ").strip().lower()
        if manual == "y":
            date_str = input("Enter date/time as MMDDhhmmYYYY: ").strip()
            run_cmd(f"date {date_str}", check=False)
    pause()

def ensure_make_conf(init_system="systemd"):
    print_section("Ensuring make.conf is present and correct")
    make_conf_path = "/mnt/gentoo/etc/portage/make.conf"
    use_flags = "openmp systemd tkip gnome -ios -ipod -ibm pipewire -pulseaudio" if init_system == "systemd" else "openrc tkip gnome -ios -ipod -ibm pipewire -pulseaudio"
    make_conf_content = f'''# These settings were set by the catalyst build script that automatically
# built this stage.
# Please consult /usr/share/portage/config/make.conf.example for a more
# detailed example.
GENTOO_MIRRORS="https://ftp-stud.hs-esslingen.de/pub/Mirrors/gentoo https://distfiles.gentoo.org http://ftp.romnet.org/gentoo/"
COMMON_FLAGS="-march=native -O2 -pipe"
CFLAGS="${{COMMON_FLAGS}}"
CXXFLAGS="${{COMMON_FLAGS}}"
FCFLAGS="${{COMMON_FLAGS}}"
FFLAGS="${{COMMON_FLAGS}}"
FEATURES="candy binpkg-request-signature getbinpkg parallel-fetch parallel-install"
MAKEOPTS="-j4 -l5"
PORTAGE_TMPDIR="/mnt/gentoo/var/tmp"
USE="{use_flags}"
CPU_FLAGS_X86="aes avx avx2 f16c fma3 mmx mmxext pclmul popcnt rdrand sse sse2 sse3 sse4_1 sse4_2 ssse3"
ACCEPT_LICENSE="@BINARY-REDISTRIBUTABLE"

# NOTE: This stage was built with the bindist USE flag enabled

# This sets the language of build output to English.
# Please keep this setting intact when reporting bugs.
LC_MESSAGES=C.utf8
GRUB_PLATFORMS="efi-64"
'''
    with open(make_conf_path, "w") as f:
        f.write(make_conf_content)
    print(f"[INFO] make.conf written to {make_conf_path}")

def ensure_package_dirs():
    import os
    portage_dir = "/mnt/gentoo/etc/portage"
    for sub in ["package.use", "package.unmask"]:
        path = os.path.join(portage_dir, sub)
        if not os.path.exists(path):
            os.makedirs(path)
            print(f"[INFO] Created {path} as directory.")

def add_package_use(pkg, useflags):
    ensure_package_dirs()
    usefile = "/mnt/gentoo/etc/portage/package.use/auto"
    with open(usefile, "a") as f:
        f.write(f"{pkg} {useflags}\n")
    print(f"[INFO] Added USE flags for {pkg} to {usefile}")

def add_package_unmask(pkg_atom):
    ensure_package_dirs()
    unmaskfile = "/mnt/gentoo/etc/portage/package.unmask/auto"
    with open(unmaskfile, "a") as f:
        f.write(f"{pkg_atom}\n")
    print(f"[INFO] Added unmask for {pkg_atom} to {unmaskfile}")

def cleanup_temp_files():
    print_section("Cleaning up temporary files and stage3 tarball")
    import os
    import glob
    # Remove /var/tmp/portage
    for path in ["/var/tmp/portage", "/mnt/gentoo/var/tmp/portage"]:
        if os.path.exists(path):
            print(f"[INFO] Removing all files in {path}")
            os.system(f"rm -rf {path}/*")
    # Remove stage3 tarball from /mnt/gentoo
    stage3_files = glob.glob("/mnt/gentoo/stage3-*.tar.xz")
    for f in stage3_files:
        print(f"[INFO] Removing stage3 tarball {f}")
        os.remove(f)

def install_stage3():
    global INIT_SYSTEM
    print_section("Downloading and Extracting Stage3")
    import re
    import urllib.request
    import os
    # Prompt user for systemd or openrc
    print("Which Gentoo stage3 do you want to install?")
    print("1) systemd (default)")
    print("2) openrc (default)")
    print("3) systemd (desktop)")
    print("4) openrc (desktop)")
    choice = input("Enter 1 for systemd, 2 for openrc, 3 for systemd (desktop) or 4 for openrc (desktop) [1]: ").strip() or "1"
    if choice == "1":
        url = "https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-systemd/"
        prefix = "stage3-amd64-systemd-"
        INIT_SYSTEM = "systemd"
    else:
        if choice == "2":
            url = "https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-openrc/"
            prefix = "stage3-amd64-openrc-"
            INIT_SYSTEM = "openrc"
        else:
            if choice == "3":
                url = "https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-systemd-desktop/"
                prefix = "stage3-amd64-systemd-desktop-"
                INIT_SYSTEM = "systemd"
            else:
                if choice == "4":
                    url = "https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-openrc-desktop/"
                    prefix = "stage3-amd64-openrc-desktop-"
                    INIT_SYSTEM = "openrc"
                else:
                    url = "https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-systemd/"
                    prefix = "stage3-amd64-systemd-"
                    INIT_SYSTEM = "systemd"
    suffix = ".tar.xz"
    print(f"[INFO] Fetching stage3 list from {url}")
    try:
        with urllib.request.urlopen(url) as response:
            html = response.read().decode()
        # Find all matching tarballs
        matches = re.findall(rf'({prefix}[\w\d\-]+{re.escape(suffix)})', html)
        if not matches:
            print("[ERROR] No stage3 tarballs found!")
            return
        # Use the last one (should be the latest)
        stage3_file = matches[-1]
        stage3_url = url + stage3_file
        print(f"[INFO] Downloading {stage3_url}")
        os.makedirs("/mnt/gentoo", exist_ok=True)
        run_cmd(f"cd /mnt/gentoo && curl -O -L {stage3_url}")
        print(f"[INFO] Extracting {stage3_file} ...")
        run_cmd(f"cd /mnt/gentoo && tar xvf {stage3_file} --xattrs")
        # Write the chroot check script line by line to avoid indentation and string literal issues
        check_script_lines = [
            "#!/usr/bin/env python3\n",
            "import os, sys\n\n",
            "def print_section(title):\n",
            "    print(\"\\n\" + \"=\"*60)\n",
            "    print(title)\n",
            "    print(\"=\"*60)\n\n",
            "def check_portage_tmpdir():\n",
            "    print_section(\"Checking PORTAGE_TMPDIR disk space and type (chroot)\")\n",
            "    tmpdir = \"/var/tmp\"\n",
            "    if not os.path.exists(tmpdir):\n",
            "        print(\"[ERROR] {} does not exist!\".format(tmpdir))\n",
            "        sys.exit(1)\n",
            "    if not os.path.isdir(tmpdir):\n",
            "        print(\"[ERROR] {} is not a directory!\".format(tmpdir))\n",
            "        sys.exit(1)\n",
            "    if os.path.islink(tmpdir):\n",
            "        print(\"[ERROR] {} is a symlink! Please make it a real directory on disk.\".format(tmpdir))\n",
            "        sys.exit(1)\n",
            "    try:\n",
            "        with open(\"/proc/mounts\") as f2:\n",
            "            mounts = f2.readlines()\n",
            "        found = False\n",
            "        for line in mounts:\n",
            "            parts = line.split()\n",
            "            if len(parts) < 3:\n",
            "                continue\n",
            "            mount_point = parts[1]\n",
            "            fs_type = parts[2]\n",
            "            if os.path.abspath(tmpdir).startswith(mount_point):\n",
            "                if fs_type in (\"tmpfs\", \"overlay\", \"aufs\", \"ramfs\"):\n",
            "                    print(\"[ERROR] {} is on {}! Please mount it on a real disk partition.\".format(tmpdir, fs_type))\n",
            "                    sys.exit(1)\n",
            "                found = True\n",
            "        if not found:\n",
            "            print(\"[WARN] Could not determine filesystem type for {}. Proceeding, but check manually if issues arise.\".format(tmpdir))\n",
            "    except Exception as e:\n",
            "        print(\"[WARN] Could not check /proc/mounts: {}\".format(e))\n",
            "    statvfs = os.statvfs(tmpdir)\n",
            "    free_gb = statvfs.f_frsize * statvfs.f_bavail / (1024**3)\n",
            "    if free_gb < 10:\n",
            "        print(\"[ERROR] {} has only {:.1f}GB free. At least 10GB is recommended for Gentoo builds.\".format(tmpdir, free_gb))\n",
            "        sys.exit(1)\n",
            "    print(\"[OK] {} is on a real disk and has {:.1f}GB free.\".format(tmpdir, free_gb))\n\n",
            "if __name__ == \"__main__\":\n",
            "    check_portage_tmpdir()\n"
        ]
        with open("/mnt/gentoo/root/check_portage_tmpdir.py", "w") as f:
            f.writelines(check_script_lines)
        os.chmod("/mnt/gentoo/root/check_portage_tmpdir.py", 0o755)
        print("[INFO] Wrote /mnt/gentoo/root/check_portage_tmpdir.py for chroot disk check.")
    except Exception as e:
        print(f"[ERROR] Failed to download or extract stage3: {e}")
    # Ensure make.conf and package dirs after stage3 extraction
    ensure_make_conf(INIT_SYSTEM)
    ensure_package_dirs()
    cleanup_temp_files()
    pause()

def setup_binpkg():
    print_section("Binary Package (binpkg) Support")
    print("Gentoo can use pre-built binary packages to speed up installation and updates.")
    print("This is especially useful for large packages or slow machines.")
    use_binpkg = yesno("Do you want to enable binary package support (binpkg)?")
    if not use_binpkg:
        return
    # Set make.conf path
    import os
    make_conf_path = "/mnt/gentoo/etc/portage/make.conf" if os.path.exists("/mnt/gentoo/etc/portage") else "/etc/portage/make.conf"
    features_line = 'FEATURES="binpkg-request-signature getbinpkg parallel-fetch parallel-install"\n'
    binhost_line = 'BINHOST="https://distfiles.gentoo.org/releases/amd64/binpackages/17.1/x86-64-v3/"\n'
    # Append to make.conf
    with open(make_conf_path, "a") as f:
        f.write("\n# Enable binary package support (binpkg)\n")
        f.write(features_line)
        f.write(binhost_line)
    print(f"[INFO] Binpkg FEATURES and BINHOST added to {make_conf_path}")

def configure_make_conf():
    print_section("Configuring make.conf")
    print("Edit /mnt/gentoo/etc/portage/make.conf as needed.")
    run_cmd("nano -w /mnt/gentoo/etc/portage/make.conf")
    pause()

def select_mirrors():
    print_section("Selecting Gentoo Mirrors")
    run_cmd("mirrorselect -D -s4 -o >> /mnt/gentoo/etc/portage/make.conf")
    pause()

def configure_repos():
    print_section("Configuring Gentoo Repos")
    run_cmd("mkdir -p /mnt/gentoo/etc/portage/repos.conf")
    run_cmd("cp /mnt/gentoo/usr/share/portage/config/repos.conf /mnt/gentoo/etc/portage/repos.conf/gentoo.conf")
    pause()

def copy_dns():
    print_section("Copying DNS Info")
    run_cmd("cp --dereference /etc/resolv.conf /mnt/gentoo/etc/")
    pause()

def mount_pseudo():
    print_section("Mounting Pseudo Filesystems")
    run_cmd("mount --types proc /proc /mnt/gentoo/proc")
    run_cmd("mount --rbind /sys /mnt/gentoo/sys")
    run_cmd("mount --make-rslave /mnt/gentoo/sys")
    run_cmd("mount --rbind /dev /mnt/gentoo/dev")
    run_cmd("mount --make-rslave /mnt/gentoo/dev")
    run_cmd("mount --bind /run /mnt/gentoo/run")
    run_cmd("mount --make-slave /mnt/gentoo/run")
    pause()

def chroot_env():
    print_section("Entering Chroot Environment")
    print("You are about to chroot into /mnt/gentoo. Continue with the next steps inside the chroot.")
    # Run the check script automatically after chroot
    chroot_check = (
        "chroot /mnt/gentoo /bin/bash -c '"
        "python3 /root/check_portage_tmpdir.py || (echo \"[FATAL] /var/tmp is not on a real disk. Aborting.\"; exit 1) && "
        "source /etc/profile; export PS1=\"(chroot) $PS1\"'"
    )
    run_cmd(chroot_check)
    print("[INFO] Now inside chroot. Continue with the following steps.")
    # After chroot, ensure /var/tmp and /tmp are on disk
    chroot_fix_tmp = (
        "chroot /mnt/gentoo /bin/bash -c 'python3 -c \"import sys; exec(\\\"""\n"
        "import os, subprocess\n"
        "def fix():\n"
        "    for path in [\\\"/var/tmp\\\", \\\"/tmp\\\"]:\n"
        "        try:\n"
        "            out = subprocess.check_output([\\\"findmnt\\\", \\\"-n\\\", \\\"-o\\\", \\\"FSTYPE\\\", path], text=True).strip()\n"
        "            if out in (\\\"tmpfs\\\", \\\"ramfs\\\", \\\"overlay\\\"):\n"
        "                subprocess.run([\\\"umount\\\", \\\"-lf\\\", path], check=False)\n"
        "                if not os.path.exists(path):\n"
        "                    os.makedirs(path, exist_ok=True)\n"
        "                else:\n"
        "                    for f in os.listdir(path):\n"
        "                        fp = os.path.join(path, f)\n"
        "                        if os.path.isdir(fp):\n"
        "                            subprocess.run([\\\"rm\\\", \\\"-rf\\\", fp])\n"
        "                        else:\n"
        "                            os.remove(fp)\n"
        "        except Exception as e:\n"
        "            pass\n"
        "        os.chmod(path, 0o1777)\n"
        "    make_conf = \\\"/etc/portage/make.conf\\\"\n"
        "    if os.path.exists(make_conf):\n"
        "        with open(make_conf, \\\"r\\\") as f:\n"
        "            lines = f.readlines()\n"
        "        found = False\n"
        "        for i, line in enumerate(lines):\n"
        "            if line.strip().startswith(\\\"PORTAGE_TMPDIR\\\"):\n"
        "                lines[i] = 'PORTAGE_TMPDIR=\\\"/var/tmp\\\"\\n'\n"
        "                found = True\n"
        "        if not found:\n"
        "            lines.append('PORTAGE_TMPDIR=\\\"/var/tmp\\\"\\n')\n"
        "        with open(make_conf, \\\"w\\\") as f:\n"
        "            f.writelines(lines)\n"
        "fix()\n"
        "\"\"\"\"'"
    )
    run_cmd(chroot_fix_tmp)
    pause()

def emerge_sync():
    print_section("Syncing Portage Tree")
    run_cmd("emerge --sync")
    pause()

def select_profile():
    print_section("Selecting Portage Profile")
    import subprocess
    global INIT_SYSTEM
    if INIT_SYSTEM:
        print(f"[INFO] Filtering profiles for {INIT_SYSTEM}...")
        filter_str = INIT_SYSTEM
        result = subprocess.run(f"eselect profile list | grep {filter_str}", shell=True, capture_output=True, text=True)
        filtered = result.stdout.strip()
        if not filtered:
            print(f"[WARN] No profiles found for {filter_str}. Showing all profiles.")
            run_cmd("eselect profile list")
        else:
            print(filtered)
    else:
        run_cmd("eselect profile list")
    idx = input("Enter the number of the desired profile: ").strip()
    run_cmd(f"eselect profile set {idx}")
    pause()

def configure_use_flags():
    print_section("Configuring USE Flags")
    ensure_toolchain_and_headers()
    run_cmd("emerge app-portage/eix app-portage/ufed")
    run_cmd("ufed")
    pause()

def configure_cpu_flags():
    print_section("Configuring CPU Flags")
    run_cmd("emerge --ask app-portage/cpuid2cpuflags")
    run_cmd("echo '*/* $(cpuid2cpuflags)' > /etc/portage/package.use/00cpu-flags")
    run_cmd("nano /etc/portage/make.conf")
    pause()

def ensure_gcc_openmp():
    print_section("Ensuring GCC has OpenMP support")
    add_package_use("sys-devel/gcc", "openmp")
    run_cmd("emerge --ask --oneshot sys-devel/gcc")
    # Find the latest GCC version and set it as default
    import subprocess
    result = subprocess.run("gcc-config -l | tail -n1 | awk '{print $2}'", shell=True, capture_output=True, text=True)
    gcc_profile = result.stdout.strip()
    if gcc_profile:
        print(f"[INFO] Setting active GCC profile to {gcc_profile}")
        run_cmd(f"gcc-config {gcc_profile}")
        run_cmd("env-update")
        run_cmd("source /etc/profile", shell=True)
    else:
        print("[WARN] Could not determine GCC profile to set.")
    # Check if OpenMP is enabled
    result = subprocess.run("gcc -v 2>&1 | grep enable-libgomp", shell=True, capture_output=True, text=True)
    if "--enable-libgomp" in result.stdout:
        print("[INFO] GCC OpenMP support is enabled.")
    else:
        print("[ERROR] GCC does not have OpenMP support. Please check your GCC build.")

def ensure_kernel_sources():
    print_section("Ensuring kernel sources are installed and symlinked")
    import os
    run_cmd("emerge --ask sys-kernel/gentoo-sources")
    # Find available kernels
    result = run_cmd("eselect kernel list", check=False, shell=True)
    print("[INFO] Please select the kernel to use for /usr/src/linux symlink.")
    idx = input("Enter the number of the kernel to use: ").strip()
    run_cmd(f"eselect kernel set {idx}")
    print("[INFO] Kernel sources installed and symlinked.")

def ensure_toolchain_and_headers():
    print_section("Ensuring toolchain and kernel headers are present")
    run_cmd("emerge --ask sys-kernel/linux-headers")
    run_cmd("emerge --ask sys-devel/autoconf sys-devel/automake sys-devel/libtool")

def update_world():
    print_section("Updating @world")
    # Ensure GCC OpenMP and kernel sources before world update
    ensure_gcc_openmp()
    ensure_kernel_sources()
    ensure_toolchain_and_headers()
    cleanup_temp_files()
    run_cmd("emerge --ask --verbose --update --deep --newuse @world")
    cleanup_temp_files()
    pause()

def configure_base_system():
    print_section("Configuring Base System")
    run_cmd("nano /etc/portage/make.conf")
    run_cmd("tzselect")
    run_cmd("ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime")
    run_cmd("nano -w /etc/locale.gen")
    run_cmd("locale-gen")
    run_cmd("eselect locale list")
    idx = input("Enter the number of the desired locale: ").strip()
    run_cmd(f"eselect locale set {idx}")
    run_cmd("env-update && source /etc/profile && export PS1=\"(chroot) $PS1\"")
    pause()

def install_kernel():
    print_section("Installing Kernel and Firmware")
    run_cmd("emerge --ask sys-kernel/gentoo-sources")
    run_cmd("eselect kernel list")
    idx = input("Enter the number of the kernel to use: ").strip()
    run_cmd(f"eselect kernel set {idx}")
    run_cmd("emerge --ask sys-kernel/linux-firmware")
    if yesno("Use genkernel for kernel build?"):
        run_cmd("emerge --ask sys-kernel/genkernel")
        run_cmd("nano -w /etc/genkernel.conf")
        run_cmd("genkernel all")
    else:
        run_cmd("emerge --ask sys-apps/pciutils")
        run_cmd("cd /usr/src/linux && make menuconfig")
        run_cmd("cd /usr/src/linux && make && make modules_install && make install")
        run_cmd("emerge --ask sys-kernel/dracut")
        run_cmd("dracut --kver=$(ls /lib/modules | tail -n1)")
    pause()

def configure_lvm():
    print_section("Configuring LVM")
    run_cmd("emerge --ask sys-fs/lvm2")
    run_cmd("nano -w /etc/lvm/lvm.conf")
    pause()

def configure_fstab():
    print_section("Configuring fstab")
    run_cmd(f"blkid {DEVICES['root']} | awk '{{print $2}}' | sed 's/\"//g'")
    if DEVICES["home"]:
        run_cmd(f"blkid {DEVICES['home']} | awk '{{print $2}}' | sed 's/\"//g'")
    if DEVICES["swap"]:
        run_cmd(f"blkid {DEVICES['swap']} | awk '{{print $2}}' | sed 's/\"//g'")
    run_cmd("nano -w /etc/fstab")
    pause()

def configure_mtab():
    print_section("Configuring mtab")
    run_cmd("ln -sf /proc/self/mounts /etc/mtab")
    pause()

def install_bootloader():
    print_section("Installing systemd-boot Bootloader")
    run_cmd("emerge --ask sys-libs/efivar")
    run_cmd("efivar -l")
    run_cmd("mount | grep boot")
    run_cmd("bootctl --path=/boot install")
    run_cmd("nano -w /boot/loader/entries/gentoo.conf")
    run_cmd("nano -w /boot/loader/loader.conf")
    run_cmd("emerge --ask sys-boot/efibootmgr")
    run_cmd("efibootmgr -v")
    if yesno("Delete all old boot entries?"):
        entry = input("Enter entry id to delete (or blank to skip): ").strip()
        if entry:
            run_cmd(f"efibootmgr -b {entry} -B")
    run_cmd("efibootmgr -c -d /dev/sda -p 1 -L 'Gentoo' -l '\\efi\\boot\\bootx64.efi'")
    pause()

def enable_lvm2():
    print_section("Enabling lvm2")
    run_cmd("systemctl enable lvm2-lvmetad.service")
    pause()

def set_root_password():
    print_section("Set Root Password")
    run_cmd("passwd")
    pause()

def add_user():
    print_section("Adding a User")
    username = input("Enter username to add: ").strip()
    run_cmd(f"useradd -m -G users,wheel,audio,video,usb -s /bin/bash {username}")
    run_cmd(f"passwd {username}")
    run_cmd("emerge --ask app-admin/sudo")
    run_cmd("visudo")
    pause()

def configure_network():
    print_section("Configuring Network")
    if yesno("Use systemd-networkd?"):
        run_cmd("nano -w /etc/systemd/network/50-dhcp.network")
        run_cmd("systemctl enable systemd-networkd.service")
        run_cmd("systemctl start systemd-networkd.service")
    else:
        run_cmd("emerge --ask networkmanager")
        run_cmd("nmtui")
    pause()

def configure_locale():
    print_section("Configuring Locale (systemd)")
    run_cmd("localectl set-locale LANG=en_US.utf8")
    run_cmd("localectl set-keymap us")
    run_cmd("localectl set-x11-keymap us")
    pause()

def configure_time():
    print_section("Configuring Time (systemd)")
    run_cmd("timedatectl set-ntp true")
    run_cmd("timedatectl status")
    run_cmd("nano -w /etc/systemd/timesyncd.conf")
    pause()

def post_install():
    print_section("Post-Installation Steps")
    run_cmd("emerge --ask sys-apps/mlocate")
    run_cmd("emerge --ask sys-fs/xfsprogs sys-fs/exfat-utils sys-fs/dosfstools sys-fs/ntfs3g")
    run_cmd("emerge --ask app-admin/sudo")
    run_cmd("emerge --ask sys-power/powertop")
    run_cmd("nano -w /etc/systemd/system/powertop.service")
    run_cmd("systemctl enable powertop.service")
    run_cmd("nano -w /etc/portage/make.conf")
    run_cmd("nano -w /etc/X11/xorg.conf.d/20-intel.conf")
    run_cmd("nano -w /etc/portage/make.conf")
    run_cmd("emerge --ask xorg-server")
    run_cmd("emerge --ask app-admin/ccze app-arch/unp app-editors/vim app-eselect/eselect-awk app-misc/screen app-shells/gentoo-zsh-completions app-vim/colorschemes app-vim/eselect-syntax app-vim/genutils app-vim/ntp-syntax media-gfx/feh sys-process/htop x11-terms/rxvt-unicode")
    run_cmd("echo 'PORTAGE_NICENESS=\"15\"' >> /etc/portage/make.conf")
    run_cmd("nano -w /etc/portage/make.conf")
    run_cmd("nano -w /etc/portage/package.accept_keywords")
    run_cmd("nano -w /etc/portage/package.unmask")
    run_cmd("nano -w /etc/portage/package.mask")
    run_cmd("emerge --ask app-portage/layman")
    run_cmd("layman -L")
    run_cmd("layman -a <overlay_name>")
    run_cmd("layman -S")
    run_cmd("mkdir -p /usr/local/portage/{metadata,profiles}")
    run_cmd("echo '<overlay_name>' > /usr/local/portage/profiles/repo_name")
    run_cmd("echo 'masters = gentoo' > /usr/local/portage/metadata/layout.conf")
    run_cmd("chown -R portage:portage /usr/local/portage")
    run_cmd("mkdir -p /etc/portage/repos.conf")
    run_cmd("nano -w /etc/portage/repos.conf/local.conf")
    run_cmd("grep --color -E 'vmx|svm' /proc/cpuinfo")
    run_cmd("ls /dev/kvm")
    run_cmd("emerge --askv app-emulation/qemu")
    run_cmd("gpasswd -a <username> kvm")
    run_cmd("systemctl enable libvirtd.service")
    run_cmd("emerge --askv prelink")
    run_cmd("env-update")
    run_cmd("nano /etc/prelink.conf")
    run_cmd("prelink -amR")
    pause()

def check_portage_tmpdir():
    print_section("Checking PORTAGE_TMPDIR disk space and type")
    tmpdir = "/mnt/gentoo/var/tmp"
    # Check if path exists
    if not os.path.exists(tmpdir):
        try:
            os.makedirs(tmpdir)
        except Exception as e:
            print(f"[ERROR] Could not create {tmpdir}: {e}")
            sys.exit(1)
    # Check if it's a directory
    if not os.path.isdir(tmpdir):
        print(f"[ERROR] {tmpdir} is not a directory!")
        sys.exit(1)
    # Check if it's a symlink
    if os.path.islink(tmpdir):
        print(f"[ERROR] {tmpdir} is a symlink! Please make it a real directory on disk.")
        sys.exit(1)
    # Check if it's on tmpfs or overlay
    try:
        with open("/proc/mounts") as f:
            mounts = f.readlines()
        found = False
        for line in mounts:
            parts = line.split()
            if len(parts) < 3:
                continue
            mount_point = parts[1]
            fs_type = parts[2]
            if os.path.abspath(tmpdir).startswith(mount_point):
                if fs_type in ("tmpfs", "overlay", "aufs", "ramfs"):  # add more if needed
                    print(f"[ERROR] {tmpdir} is on {fs_type}! Please mount it on a real disk partition.")
                    sys.exit(1)
                found = True
        if not found:
            print(f"[WARN] Could not determine filesystem type for {tmpdir}. Proceeding, but check manually if issues arise.")
    except Exception as e:
        print(f"[WARN] Could not check /proc/mounts: {e}")
    # Check free space
    statvfs = os.statvfs(tmpdir)
    free_gb = statvfs.f_frsize * statvfs.f_bavail / (1024**3)
    if free_gb < 10:
        print(f"[ERROR] {tmpdir} has only {free_gb:.1f}GB free. At least 10GB is recommended for Gentoo builds.")
        sys.exit(1)
    print(f"[OK] {tmpdir} is on a real disk and has {free_gb:.1f}GB free.")

def remount_tmpfs_if_needed():
    """Check /run and /dev/shm, and remount with larger size if they are tmpfs and nearly full."""
    import subprocess
    import re
    print_section("Checking and remounting tmpfs mounts if needed (/run, /dev/shm)")
    mounts = [('/run', 'run'), ('/dev/shm', 'dev/shm')]
    for mount_point, label in mounts:
        try:
            df_out = subprocess.check_output(['df', '-h', mount_point], text=True)
            lines = df_out.strip().split('\n')
            if len(lines) < 2:
                continue
            usage = lines[1].split()
            size = usage[1]
            used = usage[2]
            avail = usage[3]
            use_percent = int(usage[4].replace('%',''))
            fs_type = subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', mount_point], text=True).strip()
            if fs_type == 'tmpfs' and use_percent > 90:
                print(f"[WARN] {mount_point} is tmpfs and {use_percent}% full. Remounting with size=4G...")
                subprocess.run(['mount', '-o', 'remount,size=4G', mount_point], check=False)
            else:
                print(f"[INFO] {mount_point} is {fs_type} and {use_percent}% full. No remount needed.")
        except Exception as e:
            print(f"[WARN] Could not check/remount {mount_point}: {e}")

def ensure_disk_backed_tmpdirs():
    print_section("Ensuring /var/tmp and /tmp are on real disk (not tmpfs)")
    import subprocess
    import os
    for path in ["/var/tmp", "/tmp"]:
        # Check if mountpoint is tmpfs/ramfs/overlay
        try:
            out = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", path], text=True).strip()
            if out in ("tmpfs", "ramfs", "overlay"):
                print(f"[WARN] {path} is {out}, unmounting and recreating as real directory.")
                subprocess.run(["umount", "-lf", path], check=False)
                if not os.path.exists(path):
                    os.makedirs(path, exist_ok=True)
                else:
                    # Remove all files in the directory
                    for f in os.listdir(path):
                        try:
                            fp = os.path.join(path, f)
                            if os.path.isdir(fp):
                                subprocess.run(["rm", "-rf", fp])
                            else:
                                os.remove(fp)
                        except Exception:
                            pass
        except Exception as e:
            print(f"[INFO] Could not check {path}: {e}")
        # Make sure permissions are correct
        os.chmod(path, 0o1777)
    # Ensure PORTAGE_TMPDIR is set to /var/tmp
    make_conf = "/etc/portage/make.conf"
    if os.path.exists(make_conf):
        with open(make_conf, "r") as f:
            lines = f.readlines()
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("PORTAGE_TMPDIR"):
                lines[i] = 'PORTAGE_TMPDIR="/var/tmp"\n'
                found = True
        if not found:
            lines.append('PORTAGE_TMPDIR="/var/tmp"\n')
        with open(make_conf, "w") as f:
            f.writelines(lines)
        print("[INFO] Ensured PORTAGE_TMPDIR is set to /var/tmp in make.conf")

def main():
    require_root()
    print("Gentoo Automated Installer (Python)\n---")
    remount_tmpfs_if_needed()
    if yesno("Partition disk?"): partition_disk()
    prompt_device_paths()
    if yesno("Setup LUKS and LVM?"): setup_luks_lvm()
    if yesno("Create filesystems?"): create_filesystems()
    if yesno("Mount filesystems?"): mount_filesystems()
    if yesno("Set date/time?"): set_time()
    if yesno("Install stage3?"): install_stage3()
    # Ask about binpkg before make.conf
    setup_binpkg()
    if yesno("Configure make.conf?"): configure_make_conf()
    if yesno("Select mirrors?"): select_mirrors()
    if yesno("Configure repos?"): configure_repos()
    if yesno("Copy DNS info?"): copy_dns()
    if yesno("Mount pseudo filesystems?"): mount_pseudo()
    remount_tmpfs_if_needed()  # Check again before chroot/build
    if yesno("Chroot into environment?"): chroot_env()
    if yesno("Sync portage tree?"): emerge_sync()
    if yesno("Select portage profile?"): select_profile()
    if yesno("Configure USE flags?"): configure_use_flags()
    if yesno("Configure CPU flags?"): configure_cpu_flags()
    if yesno("Update @world?"): update_world()
    if yesno("Configure base system?"): configure_base_system()
    if yesno("Install kernel?"): install_kernel()
    # Only run configure_lvm if using LVM
    if USE_LVM and yesno("Configure LVM?"): configure_lvm()
    if yesno("Configure fstab?"): configure_fstab()
    if yesno("Configure mtab?"): configure_mtab()
    if yesno("Install bootloader?"): install_bootloader()
    if yesno("Enable lvm2?"): enable_lvm2()
    if yesno("Set root password?"): set_root_password()
    if yesno("Add user?"): add_user()
    if yesno("Configure network?"): configure_network()
    if yesno("Configure locale?"): configure_locale()
    if yesno("Configure time?"): configure_time()
    if yesno("Post-install steps?"): post_install()
    print("\n[INFO] Gentoo installation steps complete. Please review and continue manually if needed.")

if __name__ == "__main__":
    main() 
